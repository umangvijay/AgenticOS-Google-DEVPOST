"""Durable factory stage for generated, network-disabled MCP compute servers."""
import hashlib
import json
import uuid

from backend.mcp.builder.compute_server import generate, artifact_digest
from backend.services.isolated_runner import get_runner
from backend.services.standalone_protocol import validate_artifact
from backend.services.artifact_attestation_service import issue_attestation
from backend.services.mcp_protocol_validator import suite_digest
from backend.services.connector_revisions import checkpoint, canonical_definition_digest
from backend.services.connector_validation_service import start_validation_run, complete_validation_run
from backend.services.connector_activation import activate_revision
from backend.services.tool_validation import credential_binding_id_from, definition_digest


async def build_compute_connector(repo, *, user_id, source, name, build_id):
    request = json.loads(source)
    runner = get_runner()
    if not getattr(runner, "image", None) or not getattr(runner, "profile_digest", None):
        raise ValueError("Configure the approved isolated runner before building managed Python tools")
    artifact = generate(request, runner.image)
    # Each attempt owns a separate draft; rebuilding identical bytes must not
    # disable an existing connector or inherit its publication authority.
    publication_token = uuid.uuid4().hex
    mcp_id = hashlib.sha256((user_id + ":" + publication_token).encode()).hexdigest()[:24]
    tools = [{"tool_name": tool["name"], "description": tool["description"],
              "input_schema": tool["inputSchema"], "mcp_id": mcp_id, "mcp_version": "1.0.0",
              "risk_level": 1, "operation": {"kind": "isolated_compute", "artifact_digest": artifact["content_digest"]}}
             for tool in artifact["tools"]]
    manifest = {"mcp_id": mcp_id, "name": name or "Generated compute tools", "owner": user_id,
        "version": "1.0.0", "transport": "internal", "endpoint": "internal://standalone/" + mcp_id,
        "artifact_type": "standalone_server", "source_type": "python_tools", "auth": {"type": "NONE"},
        "spec_hash": artifact["content_digest"], "spec_json": json.dumps(artifact, sort_keys=True),
        "spec_provenance": "provided_module", "is_enabled": False, "trust_tier": "pending_review",
        "metadata": {"standalone_artifact": artifact, "runner_profile_digest": runner.profile_digest,
                     "pending_publication_token": publication_token}}
    await repo.register_mcp(manifest)
    await repo.cache_tools(mcp_id, tools)
    if build_id:
        # §29.15 revision: the standalone Forge record — the emitted
        # product identity (kind, artifact/runtime digests, per-tool
        # definition digests). Raw/effective digest are the same bytes:
        # the supplied module IS the effective source.
        try:
            from backend.services.app_intake import forge_record, \
                operations_forged, NORMALIZER_VERSION
            stored = await repo.get_build(build_id)
            intake = dict((stored or {}).get("intake") or {})
            intake["forge"] = forge_record(
                kind="standalone_stdio", method="python_tools",
                source_uri=None,
                raw_digest=artifact["content_digest"],
                effective_spec_digest=artifact["content_digest"],
                normalizer_version=NORMALIZER_VERSION,
                operations=operations_forged(tools,
                                             artifact["content_digest"]),
                spec_provenance="provided_module")
            await repo.update_build(build_id, {
                "stage": "protocol_validation", "mcp_id": mcp_id,
                "intake": intake})
        except Exception:
            await repo.update_build(build_id,
                                    {"stage": "protocol_validation",
                                     "mcp_id": mcp_id})
    validation = await validate_artifact(artifact, user_id, request.get("validation"))
    if validation["status"] != "passed":
        raise ValueError("Standalone MCP protocol validation failed")
    attestation = issue_attestation(artifact_digest=artifact["content_digest"],
        runtime_image_digest=runner.image, runner_profile_digest=runner.profile_digest,
        test_suite_digest=suite_digest(), test_run_id=validation["execution_id"],
        issuer_identity="supervisor-protocol-validator", observed_results=validation["observed"]["cases"],
        evidence_references=["runner-execution:" + validation["execution_id"]])
    # This binds execution to the configured approved, network-disabled runner.
    # It is not a claim of cloud-profile qualification.
    artifact.update(protocol_validation_status="passed", isolation_validation_status="passed")
    manifest["metadata"].update(standalone_attestations=[attestation], protocol_evidence=validation)
    manifest["trust_tier"] = "verified"
    # Retain the original definition for revision identity (evidence excluded).
    revision = await checkpoint(repo, mcp_id)
    current = await repo.get_mcp(mcp_id)
    binding = credential_binding_id_from(current["auth"])
    for tool in tools:
        run = await start_validation_run(repo, connector_id=mcp_id,
            mcp={**current, "active_revision_id": revision["revision_id"]}, tool=tool,
            credential_binding_id=binding, runtime_digest=runner.image, environment="isolated-stdio")
        fresh = await repo.get_mcp(mcp_id)
        fresh_tools = await repo.get_cached_tools(mcp_id)
        if (canonical_definition_digest(fresh, fresh_tools) != revision["revision_id"]
                or artifact_digest(fresh["metadata"]["standalone_artifact"]) != artifact["content_digest"]):
            raise ValueError("Standalone candidate changed during validation")
        await complete_validation_run(repo, run=run, passed=True,
            case_results=validation["case_results"], current_definition_digest=definition_digest(tool, fresh["spec_hash"]),
            current_credential_binding_id=credential_binding_id_from(fresh["auth"]), current_runtime_digest=runner.image)
    result = await activate_revision(repo, mcp_id=mcp_id, candidate_revision_id=revision["revision_id"],
        expected_current_revision_id=current.get("active_revision_id") or "", eligibility_allowed=True,
        actor="supervisor-protocol-validator")
    if not result["activated"]:
        raise ValueError("Standalone activation refused")
    from backend.services.execution_authority import current_authority
    authority = current_authority()
    if authority is not None and not await authority.is_current():
        raise ValueError("Workflow no longer authorizes connector publication")
    manifest["metadata"].pop("pending_publication_token")
    if not await repo.publish_managed_candidate(mcp_id, owner=user_id,
            revision_id=revision["revision_id"], publication_token=publication_token,
            metadata=manifest["metadata"]):
        raise ValueError("Standalone publication refused: candidate changed or user control intervened")
    summaries = [{"name": tool["tool_name"], "description": tool["description"]} for tool in tools]
    if build_id:
        await repo.update_build(build_id, {"status": "success", "stage": "activated", "mcp_id": mcp_id,
                                         "tools": summaries, "error": None})
    return {"status": "success", "mcp_id": mcp_id, "artifact_digest": artifact["content_digest"],
            "validation_execution_id": validation["execution_id"], "tools": summaries,
            "message": "Generated MCP compute server tested and activated on the configured isolated runner"}
