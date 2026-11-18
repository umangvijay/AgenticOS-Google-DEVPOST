"""Phase 14 §14.30–§14.33: recovery strategies, tool discovery, browser
recovery strategies, and specialist measurement — each named."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path):
    d = DatabaseManager(str(tmp_path / "strategies.db"))
    await d.initialize()
    try:
        yield d
    finally:
        await d.close()


# ── §14.30: recovery strategies measured from durable episodes ─────────

@pytest.mark.asyncio
async def test_recovery_metrics_credit_only_verified_repairs(db):
    """A recovery earns credit ONLY after the repaired outcome is
    independently verified (VERIFIED_RECOVERY) — attempted-but-unverified
    repairs never count as successes."""
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository)
    from backend.repositories.recovery_episode_repository import (
        RecoveryState)
    from backend.services.recovery_improvement import recovery_metrics
    repo = RecoveryEpisodeRepository(db)
    # A verified recovery: the full honest chain.
    ok = await repo.create_episode({
        "tenant_id": "u1", "actor_id": "system", "run_id": "run-14.30",
        "task_id": "t1", "failure_code": "TIMEOUT",
        "failure_category": "transient", "goal_digest": "g",
        "attempt_count": 1, "deadline_at": 99999999999})
    for st in (RecoveryState.CLASSIFIED, RecoveryState.PROPOSED,
               RecoveryState.VALIDATED, RecoveryState.READY):
        await repo.transition(ok["episode_id"], st, actor="system")
    await repo.claim_episode(ok["episode_id"], "w1")
    for st in (RecoveryState.EXECUTING, RecoveryState.VERIFYING,
               RecoveryState.VERIFIED_RECOVERY):
        await repo.transition(ok["episode_id"], st, actor="system",
                              require_live_lease=True)
    # A failed recovery for the same class.
    bad = await repo.create_episode({
        "tenant_id": "u1", "actor_id": "system", "run_id": "run-14.30b",
        "task_id": "t2", "failure_code": "TIMEOUT",
        "failure_category": "transient", "goal_digest": "g",
        "attempt_count": 2, "deadline_at": 99999999999})
    await repo.transition(bad["episode_id"], RecoveryState.CLASSIFIED,
                          actor="system")
    await repo.transition(bad["episode_id"], RecoveryState.PROPOSED,
                          actor="system")
    await repo.transition(bad["episode_id"], RecoveryState.VALIDATED,
                          actor="system")
    await repo.transition(bad["episode_id"], RecoveryState.READY,
                          actor="system")
    await repo.claim_episode(bad["episode_id"], "w1")
    await repo.transition(bad["episode_id"], RecoveryState.EXECUTING,
                          actor="system", require_live_lease=True)
    # The repair did not hold — verification refuses.
    await repo.transition(bad["episode_id"], RecoveryState.VERIFYING,
                          actor="system", require_live_lease=True)
    await repo.transition(bad["episode_id"], RecoveryState.RECOVERY_FAILED,
                          actor="verifier", require_live_lease=True)
    metrics = await recovery_metrics(repo, tenant_id="u1")
    bucket = metrics["by_failure_class"]["transient"]
    assert bucket["episodes"] == 2
    assert bucket["verified_recoveries"] == 1, \
        "only the VERIFIED repair earns credit"
    assert bucket["failed_recoveries"] == 1
    assert metrics["verified_recovery_rate"] == 0.5


@pytest.mark.asyncio
async def test_connector_revision_proposal_stays_unavailable():
    """§13.23 through §14.30: a candidate revision from failure evidence
    is created candidate_unavailable — the proposal cannot activate
    anything; the §13.3 readiness gates own activation."""
    import uuid as _uuid
    from backend.repositories.factory import RepositoryFactory
    from backend.services.recovery_improvement import (
        propose_connector_revision_improvement)
    from backend.services.connector_revisions import checkpoint
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        mcp_id = f"mcp-14.30-{_uuid.uuid4().hex[:6]}"
        await factory.mcp_repo.register_mcp({
            "mcp_id": mcp_id, "name": "acme", "version": "1.0",
            "endpoint": "internal://acme", "transport": "internal",
            "owner": "u1", "description": "test connector",
            "tools": [{"tool_name": "list_invoices"}],
        })
        await factory.mcp_repo.cache_tools(mcp_id, [
            {"tool_name": "list_invoices"}])
        revision = await checkpoint(factory.mcp_repo, mcp_id)
        assert revision.get("revision_id")
        out = await propose_connector_revision_improvement(
            mcp_repo=factory.mcp_repo, mcp_id=mcp_id,
            revision_service=__import__(
                "backend.services.connector_revisions",
                fromlist=["checkpoint"]),
            reviewed_failures=[{"task_id": "list_invoices",
                                "failure_category": "schema_drift"}],
            proposed_by="improvement:proposer")
        assert out["state"] == "candidate_unavailable"
        assert out["affected_operations"] == ["list_invoices"]
        assert "activates nothing" in out["note"]
    finally:
        await factory.shutdown()


# ── §14.31: tool discovery — the ladder is ordered, not leapt ──────────

def test_discovery_ladder_cannot_skip_gates():
    """Each readiness rung gates the next: a disabled connector never
    reaches the credential rung; missing credentials never reach the
    operations rung."""
    from backend.services.connector_eligibility import (
        CAPABILITY_NOT_READY, CAPABILITY_REGISTERED,
        evaluate_capability_readiness)
    # A build with no MCP record: NOT_READY (nothing registered yet).
    out = evaluate_capability_readiness(None, {"build_id": "b"})
    assert out["state"] == CAPABILITY_NOT_READY and out["ready"] is False
    # Enabled but unauthenticated: the gate refuses AT the rung — the
    # state stays REGISTERED and names the credential to connect.
    out2 = evaluate_capability_readiness(
        {"is_enabled": 1, "active_revision_id": "rev",
         "auth_json": "{}"},
        {"build_id": "b", "status": "success",
         "credential_reference": "acme",
         "required_operations": []})
    assert out2["state"] == CAPABILITY_REGISTERED
    assert "connect the account" in out2["next_action"]


# ── §14.32: browser recovery strategies decided honestly ───────────────

def test_browser_recovery_levels_are_honest():
    """§8.19 through §14.32: the recovery level never overstates what is
    actually recoverable — uncertain effects force manual
    reconciliation; storage state is not a live session."""
    from backend.services.browser_runtime import (
        RecoveryLevel, determine_recovery_level)
    # (member names: LIVE_CONTINUATION, AUTHENTICATED_RECONSTRUCTION,
    #  REAUTH_REQUIRED, MANUAL_RECONCILIATION_REQUIRED, UNSUPPORTED)
    # Uncertain effects ALWAYS gate to manual reconciliation.
    assert determine_recovery_level(
        live_session_exists=True, storage_state_available=True,
        storage_state_valid=True, pending_uncertain_effects=True,
        reauth_verifiable=True) is RecoveryLevel.MANUAL_RECONCILIATION_REQUIRED
    # Live session → live continuation.
    assert determine_recovery_level(
        live_session_exists=True, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=False) is RecoveryLevel.LIVE_CONTINUATION
    # No live session but VALID storage → authenticated reconstruction.
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=True,
        storage_state_valid=True, pending_uncertain_effects=False,
        reauth_verifiable=False) is RecoveryLevel.AUTHENTICATED_RECONSTRUCTION
    # Saved-but-INVALID storage is NOT reconstruction material —
    # verifiable reauth is the fallback; without it, full manual restart.
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=True,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=True) is RecoveryLevel.REAUTH_REQUIRED
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=False) is RecoveryLevel.UNSUPPORTED


# ── §14.33: specialist contracts are MEASURABLE ────────────────────────

@pytest.mark.asyncio
async def test_specialist_contracts_record_measurable_outcomes(db):
    """A completed specialist contract carries the measurement fields the
    evaluation joins on: identity, role, wall-time stamps, and the typed
    result — aggregates can be computed without re-running anything."""
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.services.agent_contracts import delegate, complete
    from backend.models.agent_task import AgentRole
    repo = AgentTaskRepository(db)
    set_agent_task_repo(repo)
    try:
        contract = await delegate(
            parent_kind="run", parent_id="run-14.33",
            role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
            goal="verify t1",
            input_data={"task_id": "t1"},
            output_schema={"required": ["verdict"]},
            acceptance_criteria=[{"kind": "non_empty",
                                  "description": "a verdict exists"}],
            allowed_tools=["evidence.read"], repo=repo)
        await complete(contract.contract_id,
                       result={"verdict": "pass", "reasoning": "ok"},
                       actor="inline", repo=repo)
        done = await repo.get_contract(contract.contract_id)
        assert done.state.value == "COMPLETED"
        # Measurable: the wall-time window + the typed result.
        assert done.created_at and done.updated_at
        assert done.updated_at >= done.created_at
        assert done.result["verdict"] == "pass"
        assert done.role is AgentRole.OUTCOME_VERIFIER
    finally:
        set_agent_task_repo(None)
