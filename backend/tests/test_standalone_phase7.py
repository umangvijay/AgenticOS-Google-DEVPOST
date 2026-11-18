"""Phase 7 §7.17/§7.19/§7.20 acceptance tests: MCP client result
preservation, trusted attestations, deployment eligibility, runner
contract, and candidate diagnostics — through service boundaries.

The §7.23 adversarial isolation matrix (host-secret reads, metadata access,
network denial) requires a proven runner backend and runs only where that
backend exists; the contract-level tests below verify everything verifiable
in-process, including the forged-evidence and replay rules.
"""
from types import SimpleNamespace

import pytest

from backend.models.mcp_artifact import (
    DeploymentStatus, IsolationValidationStatus, ProtocolValidationStatus,
    source_bundle_entry, source_bundle_digest, dependency_lock_digest,
)
from backend.services.artifact_attestation_service import (
    attestation_covers, issue_attestation, set_attestation_key,
    verify_attestation,
)
from backend.services.isolated_runner import (
    ExecutionRequest, RunnerUnavailableError, RUNTIME_PROFILES,
    get_runner,
)
from backend.services.mcp_deployment_service import evaluate_deployment_eligibility


# ── §7.19: trusted attestations ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _stable_key():
    set_attestation_key(b"test-attestation-key-phase7")


def _attestation(artifact="art-1", runtime="img-1", suite="suite-1"):
    return issue_attestation(
        artifact_digest=artifact, runtime_image_digest=runtime,
        runner_profile_digest="profile-1", test_suite_digest=suite,
        test_run_id="vr-1", issuer_identity="agentos-validation-service",
        observed_results={"initialize": "passed", "tools_list": "passed",
                          "tools_call": "passed"})


def test_attestation_round_trip_and_covers():
    att = _attestation()
    assert verify_attestation(att) is True
    assert attestation_covers(att, artifact_digest="art-1",
                              runtime_image_digest="img-1",
                              test_suite_digest="suite-1") is True


def test_forged_result_document_is_rejected():
    """§7.19/§7.21: a forged result document (supplied passing flags, edited
    fields) fails the issuer verification."""
    att = _attestation()
    forged = {**att, "observed_results": {"initialize": "passed",
                                          "tools_list": "passed",
                                          "tools_call": "passed",
                                          "forged": "all good"}}
    assert verify_attestation(forged) is False
    # A tampered digest is also rejected.
    tampered = {**att, "artifact_digest": "art-OTHER"}
    assert verify_attestation(tampered) is False


def test_evidence_for_one_artifact_cannot_authorize_another():
    att = _attestation(artifact="art-1")
    assert attestation_covers(att, artifact_digest="art-2",
                              runtime_image_digest="img-1",
                              test_suite_digest="suite-1") is False


# ── §7.20: deployment eligibility from current records ────────────────

def _artifact(**overrides):
    a = {
        "artifact_id": "art-1", "content_digest": "content-1",
        "runtime_image_digest": "img-1",
        "protocol_validation_status": ProtocolValidationStatus.PASSED.value,
        "isolation_validation_status": IsolationValidationStatus.PASSED.value,
    }
    a.update(overrides)
    return a


def test_deployment_allowed_with_full_evidence():
    from backend.services.mcp_protocol_validator import REQUIRED_PROTOCOL_CASES, suite_digest
    att = issue_attestation(
        artifact_digest="content-1", runtime_image_digest="img-1",
        runner_profile_digest="profile-1", test_suite_digest=suite_digest(),
        test_run_id="vr-1", issuer_identity="agentos-validation-service",
        observed_results={case["case"]: "passed" for case in REQUIRED_PROTOCOL_CASES})
    result = evaluate_deployment_eligibility(
        artifact=_artifact(), attestations=[att], owner_user_id="u1",
        caller_user_id="u1", isolation_profile_verified=True,
        expected_runner_profile_digest="profile-1", content_digest_now="content-1",
        operation_eligibility=[
            {"tool_name": "t1", "allowed": True}])
    assert result["allowed"] is True
    assert result["status"] == DeploymentStatus.ELIGIBLE.value


def test_content_change_after_testing_blocks_deployment():
    """§7.20 acceptance: changing any execution-relevant artifact content
    after testing blocks deployment until relevant validation repeats."""
    att = _attestation(artifact="content-1", runtime="img-1")
    result = evaluate_deployment_eligibility(
        artifact=_artifact(), attestations=[att], owner_user_id="u1",
        caller_user_id="u1", content_digest_now="content-CHANGED")
    assert result["allowed"] is False
    assert "ARTIFACT_CONTENT_CHANGED" in result["blocked_reasons"]


def test_protocol_pass_cannot_bypass_isolation():
    """§7.2 acceptance: passing protocol tests cannot bypass isolation
    requirements."""
    att = _attestation(artifact="content-1", runtime="img-1")
    result = evaluate_deployment_eligibility(
        artifact=_artifact(
            isolation_validation_status=IsolationValidationStatus.NOT_RUN.value),
        attestations=[att], owner_user_id="u1", caller_user_id="u1")
    assert result["allowed"] is False
    assert "ISOLATION_PROFILE_NOT_VERIFIED" in result["blocked_reasons"]


def test_revoked_connector_and_owner_checks():
    att = _attestation(artifact="content-1", runtime="img-1")
    revoked = evaluate_deployment_eligibility(
        artifact=_artifact(), attestations=[att], owner_user_id="u1",
        caller_user_id="u1", connector_revoked=True)
    assert "CONNECTOR_REVOKED" in revoked["blocked_reasons"]
    foreign = evaluate_deployment_eligibility(
        artifact=_artifact(), attestations=[att], owner_user_id="u1",
        caller_user_id="attacker", is_admin=False)
    assert "CALLER_NOT_AUTHORIZED" in foreign["blocked_reasons"]


def test_blocked_operations_block_deployment():
    att = _attestation(artifact="content-1", runtime="img-1")
    result = evaluate_deployment_eligibility(
        artifact=_artifact(), attestations=[att], owner_user_id="u1",
        caller_user_id="u1", operation_eligibility=[
            {"tool_name": "list", "allowed": True},
            {"tool_name": "delete", "allowed": False,
             "reason_codes": ["WRITE_VALIDATION_REQUIRED"]}])
    assert result["allowed"] is False
    assert any(r.startswith("OPERATIONS_NOT_ELIGIBLE") for r in result["blocked_reasons"])


# ── §7.11/§7.12: runner contract + profiles ───────────────────────────

def test_write_profile_is_gated_on_phase5_boundary():
    """§6.2/§7.3: the write_connector profile refuses to start until the
    Phase 5 effect boundary is verified — generated code cannot become an
    alternate route around the effect ledger."""
    with pytest.raises(RunnerUnavailableError):
        ExecutionRequest(
            execution_id="e1", tenant_id="u1", artifact_digest="content-1",
            runtime_image_digest="img-1", policy_profile_id="write_connector")


def test_default_runner_is_explicitly_unavailable():
    """§7.5: managed execution stays unavailable until a backend is proven
    against the isolation acceptance profile."""
    with pytest.raises(RunnerUnavailableError):
        asyncio_run(get_runner().submit_execution(ExecutionRequest(
            execution_id="e2", tenant_id="u1", artifact_digest="c",
            runtime_image_digest="i", policy_profile_id="protocol_test")))


def asyncio_run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else __import__("asyncio").run(coro)


def test_profiles_carry_explicit_limits():
    for name, profile in RUNTIME_PROFILES.items():
        assert profile["wall_time_seconds"] > 0
        assert profile["memory_mb"] > 0
        assert profile["deadline_enforcer"] == "supervisor"   # §7.12
    assert RUNTIME_PROFILES["protocol_test"]["network"] == "none"


# ── §7.10: candidate diagnostics ──────────────────────────────────────

def test_candidate_diagnostics_structure():
    from backend.agents.mcp_factory.code_generator import (
        analyze_candidate, candidate_is_clean)
    clean_code = (
        "import json\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "app = FastMCP('x')\n"
    )
    findings = analyze_candidate(clean_code)
    assert candidate_is_clean(findings) is True

    hostile = (
        "import subprocess\n"
        "import os\n"
        "eval('1+1')\n"
        "TOKEN = 'sk-abc123'\n"
    )
    findings = analyze_candidate(hostile)
    assert findings["unsupported_imports"] == ["subprocess"]
    assert "eval" in findings["forbidden_capability_requests"]
    assert any("embedded credential" in f for f in findings["suspicious_dynamic_behavior"])
    # A clean diagnostic never substitutes for isolation: the predicate is
    # diagnostic-only (asserted by the runner's separate unavailable state).


# ── §7.7: artifact identity ───────────────────────────────────────────

def test_source_bundle_rejects_unsafe_paths():
    with pytest.raises(ValueError):
        source_bundle_entry("/absolute/server.py", b"x")
    with pytest.raises(ValueError):
        source_bundle_entry("../../etc/passwd", b"x")


def test_content_digest_excludes_evidence_and_flags():
    """§7.7: recorded protocol tests and deploy flags stay OUT of identity —
    evidence stays bound to the content it validated."""
    from backend.models.mcp_artifact import MCPArtifact
    a = MCPArtifact(artifact_id="a1", connector_id="c1",
                    source_bundle=[source_bundle_entry("server.py", b"x")],
                    source_bundle_digest="d1")
    before = a.content_digest()
    a.protocol_validation_status = ProtocolValidationStatus.PASSED.value
    a.deployment_status = DeploymentStatus.ELIGIBLE.value
    a.attestation_ids.append("att-1")
    assert a.content_digest() == before
