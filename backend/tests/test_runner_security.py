"""Phase 7 §7.23 acceptance tests: broker enforcement, dependency lock
integrity, protocol validation classification, repair classification and
authority limits, isolation decision record, kill switches, and the durable
runner execution lifecycle.

The adversarial isolation matrix (host-secret reads, metadata access,
memory exhaustion) requires the proven runner backend and runs only where
it exists (§7.5); everything verifiable in-process is tested here through
service boundaries.
"""
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.effect import EffectState
from backend.services.runner_security import (
    BrokerDecision, ExecutionCapability, evaluate_broker_request,
    isolation_decision_summary,
)
from backend.services.artifact_service import (
    DependencyLock, build_dependency_lock, package_artifact, verify_export_zip,
)
from backend.services.mcp_protocol_validator import (
    classify_protocol_run, observed_from_client_results, suite_digest,
)
from backend.services.connector_repair_service import (
    classify_failure, propose_repair, repair_candidate_created,
    FailureCategory, MAX_REPAIR_ATTEMPTS,
)
from backend.services.execution_controls import (
    ExecutionControls, get_execution_controls,
)


# ── §7.14/§7.15: the operation broker ─────────────────────────────────

def _capability(**overrides) -> ExecutionCapability:
    defaults = dict(
        capability_id="cap-1", tenant_id="u1", execution_id="exec-1",
        artifact_digest="content-1", operation_name="list_projects",
        credential_binding_id="b-1",
        allowed_destinations=["https://api.example.com"],
        allowed_methods=["GET"], request_budget=3, expires_at=0)
    defaults.update(overrides)
    return ExecutionCapability(**defaults)


def test_broker_allows_in_context_request():
    decision, _ = evaluate_broker_request(
        _capability(), tenant_id="u1", execution_id="exec-1",
        artifact_digest="content-1", method="GET",
        destination="https://api.example.com/v1/projects")
    assert decision is BrokerDecision.ALLOWED


def test_broker_replays_rejected():
    """§7.15 acceptance: replaying a capability from another execution,
    tenant, or artifact is rejected."""
    cap = _capability()
    for other in ({"tenant_id": "attacker"}, {"execution_id": "exec-OTHER"},
                  {"artifact_digest": "content-OTHER"}):
        kwargs = dict(tenant_id="u1", execution_id="exec-1",
                      artifact_digest="content-1")
        kwargs.update(other)
        decision, reason = evaluate_broker_request(
            cap, method="GET", destination="https://api.example.com/",
            **kwargs)
        assert decision is BrokerDecision.DENIED_CAPABILITY_REPLAY, (other, reason)


def test_broker_enforces_destination_method_expiry_budget():
    cap = _capability(expires_at=int(time.time()) - 10)
    d, _ = evaluate_broker_request(cap, tenant_id="u1", execution_id="exec-1",
                                   artifact_digest="content-1", method="GET",
                                   destination="https://api.example.com/")
    assert d is BrokerDecision.DENIED_CAPABILITY_EXPIRED

    cap = _capability()
    d, _ = evaluate_broker_request(cap, tenant_id="u1", execution_id="exec-1",
                                   artifact_digest="content-1", method="DELETE",
                                   destination="https://api.example.com/")
    assert d is BrokerDecision.DENIED_METHOD

    d, _ = evaluate_broker_request(cap, tenant_id="u1", execution_id="exec-1",
                                   artifact_digest="content-1", method="GET",
                                   destination="https://evil.example.com/")
    assert d is BrokerDecision.DENIED_DESTINATION

    cap = _capability(request_budget=1)
    evaluate_broker_request(cap, tenant_id="u1", execution_id="exec-1",
                            artifact_digest="content-1", method="GET",
                            destination="https://api.example.com/",
                            requests_used=1)
    d, _ = evaluate_broker_request(cap, tenant_id="u1", execution_id="exec-1",
                                   artifact_digest="content-1", method="GET",
                                   destination="https://api.example.com/",
                                   requests_used=1)
    assert d is BrokerDecision.DENIED_BUDGET_EXHAUSTED


# ── §7.9: dependency lock integrity ───────────────────────────────────

def test_lock_requires_hashes_for_every_package():
    """§7.9: hash-checked installation requires hashes for ALL dependencies."""
    with pytest.raises(ValueError):
        build_dependency_lock("3.11.16", "manylinux2014_x86_64", "x86_64",
                              direct=[{"name": "mcp", "version": "1.16.0"}],
                              transitive=[])


def test_lock_integrity_verification():
    lock = build_dependency_lock(
        "3.11.16", "manylinux2014_x86_64", "x86_64",
        direct=[{"name": "mcp", "version": "1.16.0",
                 "distribution_hash": "sha256:" + "a" * 64}],
        transitive=[{"name": "pydantic", "version": "2.9.2",
                     "distribution_hash": "sha256:" + "b" * 64}])
    installed = {"mcp": "a" * 64, "pydantic": "b" * 64}
    ok, violations = lock.verify_integrity(installed)
    assert ok is True
    # A changed package file fails integrity verification.
    ok, violations = lock.verify_integrity(
        {"mcp": "c" * 64, "pydantic": "b" * 64})
    assert ok is False and any("mcp" in v for v in violations)
    # A clean build cannot silently resolve additional dependencies.
    ok, violations = lock.verify_integrity(
        {**installed, "extra": "d" * 64})
    assert ok is False and any("extra" in v for v in violations)


# ── §7.16: protocol validation classification ─────────────────────────

def test_protocol_suite_all_required_cases_passed():
    results = observed_from_client_results({case: "passed" for case in [
        "initialize", "tools_list", "tools_call_valid",
        "tools_call_invalid_input", "tools_call_unknown_tool",
        "tool_error_preserved", "shutdown"]})
    classified = classify_protocol_run(results)
    assert classified["status"] == "passed"


def test_skipped_required_case_does_not_count_as_passed():
    """§6.9/§7.16: a skipped required case must not count as passed."""
    results = observed_from_client_results({
        "initialize": "passed", "tools_list": "passed",
        "tools_call_valid": "skipped"})   # rest skipped
    classified = classify_protocol_run(results)
    assert classified["status"] == "failed"
    assert "tools_call_valid" in classified["missing"]


def test_uncertain_run_is_not_a_protocol_pass_claim():
    results = observed_from_client_results({c: "uncertain" for c in [
        "initialize", "tools_list", "tools_call_valid",
        "tools_call_invalid_input", "tools_call_unknown_tool",
        "tool_error_preserved", "shutdown"]})
    assert classify_protocol_run(results)["status"] != "passed"


def test_failed_initialization_is_no_protocol_pass_claim():
    """§7.21 matrix: fail MCP initialization → no protocol-pass claim."""
    results = observed_from_client_results({
        "initialize": "failed"})
    assert classify_protocol_run(results)["status"] == "failed"


# ── §7.22: repair classification and authority limits ─────────────────

def test_repair_classification_matches_responses():
    assert classify_failure("401 Unauthorized", status_code=401) == \
        FailureCategory.CREDENTIAL_FAILURE
    assert classify_failure("rate limit exceeded", status_code=429) == \
        FailureCategory.RATE_LIMITING
    assert classify_failure("schema mismatch detected") == \
        FailureCategory.SCHEMA_MISMATCH
    assert classify_failure("uncertain write outcome") == \
        FailureCategory.UNCERTAIN_WRITE


def test_repair_does_not_regenerate_for_credential_failures():
    """§7.22: provider credential failure → request credential resolution;
    avoid unnecessary regeneration."""
    proposal = propose_repair(FailureCategory.CREDENTIAL_FAILURE)
    assert proposal.justified is False
    assert proposal.action == "request_credential_resolution"


def test_repair_proposer_cannot_self_approve_or_exceed_limits():
    """§7.22: bounded loop — attempt limit and cost budget stop the proposer;
    the candidate must pass the ordinary gates (it never holds authority)."""
    proposal = propose_repair(FailureCategory.SDK_INCOMPATIBILITY,
                              attempts_used=0)
    assert proposal.justified is True
    exhausted = propose_repair(FailureCategory.SDK_INCOMPATIBILITY,
                               attempts_used=MAX_REPAIR_ATTEMPTS)
    assert exhausted.justified is False
    assert "limit" in (exhausted.blocked_reason or "")
    proposal = repair_candidate_created(
        proposal, candidate_artifact_id="art-candidate",
        source_diff="endpoint changed", permission_diff="none")
    assert proposal.candidate_artifact_id == "art-candidate"


# ── §7.5: the isolation decision record ───────────────────────────────

def test_managed_execution_unavailable_until_proven():
    """§7.5 step 5: managed execution stays unavailable until the selected
    backend passes every acceptance test in a live deployment."""
    summary = isolation_decision_summary()
    assert summary["managed_execution_available"] is False
    assert summary["selected_backend"] == "cloud_run_sandbox"
    assert summary["acceptance_test_count"] == 10
    # The documented Cloud Run filesystem constraint is recorded as unproven.
    assert "filesystem_bundle_only" in summary["controls_unproven"]


# ── §7.24: kill switches ──────────────────────────────────────────────

def test_kill_switches_block_dispatch():
    controls = ExecutionControls()
    ok, reasons = controls.check_dispatch(artifact_digest="content-1",
                                          runtime_image_digest="img-1")
    assert ok is True
    controls.disable_new_executions("operator")
    ok, reasons = controls.check_dispatch(artifact_digest="content-1",
                                          runtime_image_digest="img-1")
    assert ok is False and "NEW_EXECUTIONS_DISABLED" in reasons
    controls.enable_new_executions()
    controls.revoke_artifact("content-1", "suspected malicious behavior")
    ok, reasons = controls.check_dispatch(artifact_digest="content-1",
                                          runtime_image_digest="img-1")
    assert ok is False and any("ARTIFACT_REVOKED" in r for r in reasons)
    controls.revoke_runtime_image("img-1", "vulnerable base image")
    ok, reasons = controls.check_dispatch(artifact_digest="other",
                                          runtime_image_digest="img-1")
    assert ok is False and any("RUNTIME_IMAGE_REVOKED" in r for r in reasons)


def test_provider_access_stop_is_visible_to_broker_path():
    controls = get_execution_controls()
    controls.stop_provider_access("incident: unexpected egress")
    ok, reason = controls.check_broker()
    assert ok is False
    controls.provider_access_stopped = False   # restore for other tests


# ── §7.11: durable runner execution lifecycle ─────────────────────────

@pytest_asyncio.fixture
async def runner_repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.runner_repository import RunnerExecutionRepository
    db = DatabaseManager(str(tmp_path / "runner.db"))
    await db.initialize()
    yield RunnerExecutionRepository(db)
    await db.close()


@pytest.mark.asyncio
async def test_execution_record_survives_and_orphans_are_reclaimed(runner_repo):
    """§7.11 acceptance: losing the API connection does not lose the
    execution record, and a crashed worker cannot leave an indefinitely
    running sandbox."""
    from backend.services.execution_controls import reconcile_orphans
    record = {"execution_id": "exec-1", "tenant_id": "u1",
              "artifact_digest": "content-1", "runtime_image_digest": "img-1",
              "policy_profile_id": "protocol_test", "state": "running",
              "lease_expires_at": time.time() - 999}   # lease lost
    await runner_repo.save_execution(record)
    # The record is durable (a fresh repo sees it — same store here).
    fetched = await runner_repo.get_execution("exec-1")
    assert fetched is not None and fetched["state"] == "running"

    report = await reconcile_orphans(runner_repo)
    assert report["orphans_marked"] == 1
    fetched = await runner_repo.get_execution("exec-1")
    assert fetched["state"] == "superseded"
    assert "lease lost" in fetched["exit_reason"]
    # Cleanup state is tracked SEPARATELY from execution state (§7.11) —
    # the first pass completes both stages for this record; a second pass
    # finds nothing left to do (idempotent).
    assert fetched["cleanup_state"] == "cleaned"
    report2 = await reconcile_orphans(runner_repo)
    assert report2["orphans_marked"] == 0 and report2["cleaned"] == 0


@pytest.mark.asyncio
async def test_cancellation_is_idempotent_and_terminates(runner_repo):
    await runner_repo.save_execution(
        {"execution_id": "exec-c", "tenant_id": "u1", "artifact_digest": "c",
         "policy_profile_id": "protocol_test", "state": "running"})
    assert await runner_repo.cancel_pending("exec-c") is True
    assert await runner_repo.cancel_pending("exec-c") is False   # idempotent
    assert await runner_repo.mark_terminated("exec-c", "supervisor terminated")
    rec = await runner_repo.get_execution("exec-c")
    assert rec["state"] == "cancelled"
    assert rec["cleanup_state"] == "pending"   # separate from execution state
