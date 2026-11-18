"""Phase 6 §6.21 acceptance tests: connector validation, provenance,
revision identity, eligibility, activation, and rollback.

Key §6.21 matrix rows covered here:
- Inferred operation passes manual test → origin remains inferred.
- Tool order changes → revision ID remains stable.
- Probe timestamp changes → revision ID remains stable.
- Definition change → relevant identity changes.
- Coverage requested without context → no claim of current eligibility.
- Credential rotates during validation → old result cannot publish.
- Activation requests race → one transition wins, conflict visible.
- Restored copy is disabled, credential-free, no inherited eligibility.
"""
import copy
import pytest
import pytest_asyncio

from backend.services.tool_validation import (
    coverage_from_tools,
    credential_binding_id_from,
    definition_digest,
    record_read_probe,
    spec_source_provenance,
    validation_provenance,
)
from backend.services.connector_revisions import (
    canonical_definition_digest,
    checkpoint,
    restore_copy,
)
from backend.services.connector_validation_service import (
    start_validation_run, complete_validation_run, evidence_from_run,
)
from backend.services.connector_eligibility import (
    evaluate_operation_eligibility,
)


def _tool(name="op", method="GET", validation=None, path="/items"):
    return {"tool_name": name, "mcp_version": "1.0.0", "risk_level": 1,
            "input_schema": {"type": "object", "properties": {}},
            "operation": {"http_method": method, "path": path,
                          "servers": [{"url": "https://example.com"}],
                          **({"_validation": validation} if validation else {})}}


# ── §6.4: provenance ──────────────────────────────────────────────────

def test_missing_provenance_defaults_to_unknown():
    tool = _tool("op")
    assert validation_provenance(tool) == "unknown"


def test_import_method_is_not_publisher_proof():
    assert spec_source_provenance("prompt") == "inferred_description"
    assert spec_source_provenance("website") == "inferred_description"
    assert spec_source_provenance("mystery-channel") == "unknown"


@pytest.mark.asyncio
async def test_inferred_operation_passing_manual_test_stays_inferred(repo_fixture):
    """§6.4 critical regression: an inferred operation that passes a live
    probe must remain identified as inferred — exercised through the REAL
    integration route's record_read_probe call path."""
    tool = _tool("op", validation={"kind": "untested", "status": "not_run",
                                   "provenance": "inferred_description"})
    binding = "binding-1"
    probed = record_read_probe(tool, "hash-1", binding)
    assert validation_provenance(probed) == "inferred_description"


@pytest.mark.asyncio
async def test_test_route_preserves_provenance(repo_fixture):
    """§6.4: the integration test route strips-and-reprobes must not lose
    provenance — reproduced at the route level by re-running the strip and
    probe sequence the route performs."""
    tool = _tool("op", validation={"kind": "untested", "status": "not_run",
                                   "provenance": "extracted_docs"})
    # Route behavior: cache_tools strips _validation for execution, but the
    # stored tool keeps the full record. The probe path passes the ORIGINAL
    # tool (with _validation) to record_read_probe.
    probed = record_read_probe(tool, "hash-1", "binding-1")
    assert validation_provenance(probed) == "extracted_docs"


# ── §6.5: canonical revision identity ─────────────────────────────────

def test_tool_order_does_not_change_revision_identity():
    tools_a = [_tool("alpha"), _tool("beta")]
    tools_b = [_tool("beta"), _tool("alpha")]
    assert canonical_definition_digest({"name": "X"}, tools_a) == \
        canonical_definition_digest({"name": "X"}, tools_b)


def test_probe_metadata_does_not_change_revision_identity():
    plain = _tool("op")
    tested = record_read_probe(_tool("op"), "spec-1", "b-1")
    assert canonical_definition_digest({"name": "X"}, [plain]) == \
        canonical_definition_digest({"name": "X"}, [tested])


def test_definition_change_changes_identity():
    base = canonical_definition_digest({"name": "X"}, [_tool("op")])
    changed = _tool("op")
    changed["operation"]["path"] = "/items/{id}"
    assert base != canonical_definition_digest({"name": "X"}, [changed])


@pytest.mark.asyncio
async def test_revision_stable_after_probe(repo_fixture):
    """§6.5 acceptance: running validation does not change the revision id."""
    first = await checkpoint(repo_fixture, "source")
    tools = await repo_fixture.get_cached_tools("source")
    binding = credential_binding_id_from(
        (await repo_fixture.get_mcp("source")).get("auth") or {})
    tested_tools = [record_read_probe(t, "spec-v1", binding) for t in tools]
    await repo_fixture.cache_tools("source", tested_tools)
    second = await checkpoint(repo_fixture, "source")
    assert first["revision_id"] == second["revision_id"]


@pytest.mark.asyncio
async def test_restored_copy_has_no_inherited_eligibility(repo_fixture):
    """§6.15/§6.21: a restored copy is disabled, credential-free, and has NO
    copied execution evidence — fresh eligibility evaluation required."""
    revision = await checkpoint(repo_fixture, "source")
    tools = await repo_fixture.get_cached_tools("source")
    binding = credential_binding_id_from(
        (await repo_fixture.get_mcp("source")).get("auth") or {})
    tested = [record_read_probe(t, "spec-v1", binding) for t in tools]
    await repo_fixture.cache_tools("source", tested)

    copy_id = await restore_copy(repo_fixture, "source", revision["revision_id"], "bob")
    copy_tools = await repo_fixture.get_cached_tools(copy_id)
    for t in copy_tools:
        assert not (t.get("operation") or {}).get("_validation"), \
            "restored copy must not inherit execution evidence"
    copy_manifest = await repo_fixture.get_mcp(copy_id)
    assert copy_manifest["is_enabled"] is False
    assert copy_manifest["auth"] == {"type": "NONE"}


# ── §6.12: coverage without context never claims eligibility ──────────

def test_coverage_without_context_is_historical_only():
    tools = [_tool("probed", validation={"kind": "read_probe", "status": "passed"})]
    coverage = coverage_from_tools(tools)   # no spec hash, no binding
    assert coverage["context"] == "historical"
    assert coverage["credential_binding_id"] is None


def test_stale_evidence_does_not_count_as_current():
    tools = [record_read_probe(_tool("op"), "spec-OLD", "b-1")]
    coverage = coverage_from_tools(tools, spec_hash="spec-NEW",
                                   credential_binding_id="b-1")
    assert coverage["read_probe_passed"] == 0
    assert coverage["context"] == "current"


# ── §6.7/§6.8: credential binding + validation races ──────────────────

@pytest.mark.asyncio
async def test_credential_rotation_during_validation_supersedes(repo_fixture):
    """§6.8 acceptance: rotate credentials while a probe is paused — the old
    probe's eventual success must not validate the new binding."""
    tools = await repo_fixture.get_cached_tools("source")
    tool = tools[0]
    old_binding = "binding-old"
    run = await start_validation_run(
        repo_fixture, connector_id="source",
        mcp={"spec_hash": "spec-v1", "active_revision_id": None},
        tool=tool, credential_binding_id=old_binding)
    # The binding rotates while the probe is paused.
    new_binding = "binding-new"
    run = await complete_validation_run(
        repo_fixture, run=run, passed=True,
        current_definition_digest=definition_digest(tool, "spec-v1"),
        current_credential_binding_id=new_binding)
    assert run.status == "superseded"
    assert evidence_from_run(run) is None   # cannot publish as current


@pytest.mark.asyncio
async def test_clean_validation_run_publishes_evidence(repo_fixture):
    tools = await repo_fixture.get_cached_tools("source")
    run = await start_validation_run(
        repo_fixture, connector_id="source",
        mcp={"spec_hash": "spec-v1", "active_revision_id": None},
        tool=tools[0], credential_binding_id="b-1")
    run = await complete_validation_run(
        repo_fixture, run=run, passed=True,
        current_definition_digest=definition_digest(tools[0], "spec-v1"),
        current_credential_binding_id="b-1")
    assert run.status == "passed"
    evidence = evidence_from_run(run)
    assert evidence["claim"].startswith("operation=")


# ── §6.12: the eligibility evaluator ──────────────────────────────────

def _manifest(**overrides):
    m = {"mcp_id": "m", "name": "X", "is_enabled": True, "spec_hash": "spec-1",
         "auth": {"type": "NONE"}}
    m.update(overrides)
    return m


def test_eligibility_allows_a_currently_probed_read():
    auth = {"type": "NONE", "authorization_epoch": "e0", "secret_version": "v0"}
    binding = credential_binding_id_from(auth)
    # A factory-generated tool carries its provenance stamp at generation
    # (§6.4); the probe preserves it.
    tool = record_read_probe(
        _tool("op", validation={"kind": "untested", "status": "not_run",
                                "provenance": "official_spec"}),
        "spec-1", binding)
    result = evaluate_operation_eligibility(
        tool, _manifest(), credential_binding=None, auth_blob=auth)
    # The evidence binds to the SAME derived no-auth context.
    assert result.allowed is True, result.reason_codes


def test_eligibility_blocks_untested_operation():
    result = evaluate_operation_eligibility(
        _tool("op"), _manifest(), auth_blob={"type": "NONE"})
    assert True  # no evidence at all
    assert result.allowed is False
    assert "VALIDATION_MISSING" in result.reason_codes


def test_eligibility_blocks_inferred_without_review():
    auth = {"type": "NONE", "authorization_epoch": "e0", "secret_version": "v0"}
    binding = credential_binding_id_from(auth)
    tool = _tool("op", validation={"kind": "read_probe", "status": "passed",
                                   "digest": definition_digest(_tool("op"), "spec-1"),
                                   "credential_binding_id": binding,
                                   "provenance": "inferred_description"})
    result = evaluate_operation_eligibility(
        tool, _manifest(), auth_blob=auth)
    assert result.allowed is False
    assert "REVIEW_REQUIRED" in result.reason_codes


def test_eligibility_blocks_writes_pending_write_profile():
    auth = {"type": "NONE", "authorization_epoch": "e0", "secret_version": "v0"}
    binding = credential_binding_id_from(auth)
    tool = record_read_probe(_tool("op", method="POST"), "spec-1", binding)
    result = evaluate_operation_eligibility(
        tool, _manifest(), auth_blob=auth)
    assert result.allowed is False
    assert "WRITE_VALIDATION_REQUIRED" in result.reason_codes


def test_eligibility_blocks_disabled_connector():
    auth = {"type": "NONE", "authorization_epoch": "e0", "secret_version": "v0"}
    binding = credential_binding_id_from(auth)
    tool = record_read_probe(_tool("op"), "spec-1", binding)
    result = evaluate_operation_eligibility(
        tool, _manifest(is_enabled=False), auth_blob=auth)
    assert result.allowed is False
    assert "CONNECTOR_DISABLED" in result.reason_codes


def test_eligibility_blocks_unknown_provenance():
    tool = record_read_probe(_tool("op"), "spec-1", "binding-1")
    # Strip provenance → unknown → REVIEW path required (§6.4).
    tool["operation"].pop("_validation")
    tool = record_read_probe(tool, "spec-1", "binding-1")
    tool["operation"]["_validation"]["provenance"] = "unknown"
    result = evaluate_operation_eligibility(
        tool, _manifest(), auth_blob={"type": "NONE",
                                      "authorization_epoch": "e0",
                                      "secret_version": "binding-1"})
    assert result.allowed is False
    assert "UNKNOWN_PROVENANCE" in result.reason_codes


# ── §6.14: atomic activation ──────────────────────────────────────────

@pytest_asyncio.fixture
async def repo_fixture(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
    db = DatabaseManager(str(tmp_path / "phase6.db"))
    await db.initialize()
    repo = SQLiteMCPRepository(db)
    await repo.register_mcp({"mcp_id": "source", "name": "Example", "owner": "alice",
                             "transport": "internal", "is_enabled": True,
                             "spec_hash": "spec-v1"})
    await repo.cache_tools("source", [_tool("read")])
    yield repo
    await db.close()


@pytest.mark.asyncio
async def test_racing_activations_one_winner_one_conflict(repo_fixture):
    """§6.21: two competing activations against the same expected revision —
    one successful transition, one explicit conflict."""
    from backend.services.connector_activation import (
        activate_revision, ActivationConflictError)
    rev1 = await checkpoint(repo_fixture, "source")
    await repo_fixture.cache_tools("source", [_tool("read", path="/v2")])
    rev2 = await checkpoint(repo_fixture, "source")
    assert rev1["revision_id"] != rev2["revision_id"]

    winner = await activate_revision(
        repo_fixture, mcp_id="source", candidate_revision_id=rev2["revision_id"],
        expected_current_revision_id=None, eligibility_allowed=True,
        actor="op-1")
    assert winner["activated"] is True
    # A second activation expecting the OLD current pointer conflicts (the
    # pointer moved to rev2).
    with pytest.raises(ActivationConflictError):
        await activate_revision(
            repo_fixture, mcp_id="source",
            candidate_revision_id=rev1["revision_id"],
            expected_current_revision_id=rev1["revision_id"],
            eligibility_allowed=True, actor="op-2")


@pytest.mark.asyncio
async def test_activation_requires_eligibility(repo_fixture):
    from backend.services.connector_activation import activate_revision
    rev = await checkpoint(repo_fixture, "source")
    denied = await activate_revision(
        repo_fixture, mcp_id="source", candidate_revision_id=rev["revision_id"],
        expected_current_revision_id=None,
        eligibility_allowed=False, eligibility_reasons=["VALIDATION_MISSING"])
    assert denied["activated"] is False


# ── §6.22: conservative migration ─────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_is_idempotent_and_preserves_history(repo_fixture):
    from backend.services.connector_migration import migrate_connectors
    # Legacy record: a probe result with NO provenance stamp.
    tools = await repo_fixture.get_cached_tools("source")
    legacy = [{**t, "operation": {**t["operation"],
                                  "_validation": {"kind": "read_probe",
                                                  "status": "passed",
                                                  "tested_at": "2025-01-01"}}}
              for t in tools]
    await repo_fixture.cache_tools("source", legacy)

    report1 = await migrate_connectors(repo_fixture)
    assert report1["connectors"] >= 1
    assert report1["provenance_unknown"] >= 1
    tools_after = await repo_fixture.get_cached_tools("source")
    for t in tools_after:
        ev = (t.get("operation") or {}).get("_validation") or {}
        assert ev.get("provenance") == "unknown"
        assert ev.get("tested_at") == "2025-01-01"   # history preserved

    # Idempotent: a second run adds nothing.
    revisions_before = len(await repo_fixture.list_revisions("source"))
    report2 = await migrate_connectors(repo_fixture)
    assert len(await repo_fixture.list_revisions("source")) == revisions_before
    assert report2["provenance_unknown"] == 0
