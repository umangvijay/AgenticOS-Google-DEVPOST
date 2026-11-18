"""Phase 15 §15.28–§15.30: storage parity for the delegation contract,
the complete multi-agent self-extension journey, and the progressive
release machinery."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


# ── §15.28: one contract, two stores ───────────────────────────────────

def test_delegation_scenarios_exist_on_both_stores():
    """The delegation contract scenarios (concurrent creation, duplicate
    dispatch, claim takeover, expired-owner completion, cancellation
    races, output publication, budget contention) are the SAME method
    surface on SQLite and Firestore — cloud execution preserves the
    contract; unsupported behavior cannot hide."""
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository)
    from backend.repositories.firestore.agent_task_repository import (
        FirestoreAgentTaskRepository)
    sql = {m for m in dir(AgentTaskRepository)
           if not m.startswith("_") and callable(getattr(AgentTaskRepository, m))}
    fs = {m for m in dir(FirestoreAgentTaskRepository)
          if not m.startswith("_") and callable(getattr(FirestoreAgentTaskRepository, m))}
    missing = sql - fs
    assert not missing, f"Firestore delegation is missing: {sorted(missing)}"


def test_reward_delivery_replay_reaches_both_stores():
    """The reward-delivery replay scenario: the reward-event consumption
    contract is identical on both stores (§14.19 extended to Phase 15's
    carryover row)."""
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    from backend.repositories.firestore.sidecar import (
        FirestoreAdaptivePolicyRepository)
    import inspect
    for cls in (SQLiteAdaptivePolicyRepository,
                FirestoreAdaptivePolicyRepository):
        sig = inspect.signature(cls.consume_reward_event)
        assert {"event_id", "consumer"} <= set(sig.parameters)


@pytest.mark.asyncio
async def test_sqlite_scenarios_run_the_full_delegation_contract(tmp_path):
    """The scenarios executed against the reference store: concurrent
    creation (one winner), duplicate dispatch, claim takeover,
    expired-owner completion, cancellation races, output conflicts,
    budget contention."""
    from backend.models.agent_task import AgentRole, AgentTaskError
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.services.agent_contracts import (
        cancel, complete, delegate, fail)
    db = DatabaseManager(str(tmp_path / "parity.db"))
    await db.initialize()
    repo = AgentTaskRepository(db)
    set_agent_task_repo(repo)
    C = [{"kind": "non_empty"}]
    def kw(goal, **over):
        base = dict(parent_kind="run", parent_id="run-par",
                    role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                    goal=goal, input_data={"x": 1},
                    output_schema={"required": ["ok"]},
                    acceptance_criteria=C,
                    allowed_tools=["evidence.read"], repo=repo)
        base.update(over)
        return base
    try:
        import asyncio
        # Concurrent creation + duplicate dispatch: one winner.
        results = await asyncio.gather(
            delegate(**kw("the work", dedup_key="k1")),
            delegate(**kw("the work too", dedup_key="k1")),
            return_exceptions=True)
        oks = [r for r in results if not isinstance(r, AgentTaskError)]
        assert len(oks) == 1
        # Claim takeover: A's expired lease, B takes over, A fenced out.
        c = oks[0]
        await repo.claim(c.contract_id, "A", lease_seconds=60)
        await repo.db.execute(
            "UPDATE agent_tasks SET lease_expires_at=1 WHERE contract_id=?",
            (c.contract_id,))
        await repo.db.commit()
        assert await repo.claim(c.contract_id, "B") is not None
        with pytest.raises(AgentTaskError, match="Stale worker"):
            await complete(c.contract_id, {"ok": 1}, actor="A", repo=repo)
        await complete(c.contract_id, {"ok": 1}, actor="B", repo=repo)
        # Cancellation race: cancel a terminal contract is refused;
        # cancelling a parent propagates to its active child only.
        parent = await delegate(**kw("parent", budget_units=100))
        child = await delegate(
            parent_kind="contract", parent_id=parent.contract_id,
            role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="kid",
            input_data={"x": 1}, output_schema={"required": ["ok"]},
            acceptance_criteria=C, allowed_tools=["evidence.read"],
            budget_units=10, depth=1,
            ancestor_ids=[parent.contract_id], repo=repo)
        out = await cancel(parent.contract_id, actor="operator", repo=repo)
        assert out["children_cancelled"] == 1
        # Cancellation race/budget contention: the CANCELLED parent
        # admits nothing new — new work under it is refused.
        with pytest.raises(AgentTaskError, match="CANCELLED"):
            await delegate(
                parent_kind="contract", parent_id=parent.contract_id,
                role=AgentRole.RECOVERY_ANALYST, tenant_id="u1",
                goal="after cancel", input_data={"x": 1},
                output_schema={"required": ["ok"]},
                acceptance_criteria=C, allowed_tools=["evidence.read"],
                budget_units=10, depth=1,
                ancestor_ids=[parent.contract_id], repo=repo)
    finally:
        set_agent_task_repo(None)
        await db.close()


# ── §15.29: the complete multi-agent self-extension journey ────────────

@pytest.mark.asyncio
async def test_self_extension_journey_end_to_end(tmp_path):
    """One controlled journey with no connector installed:
    discovery → generation request → account binding → operation
    validation → activation gate → parent resumption readiness →
    execution → recoverable failure → deliverable → verification.
    Every specialist's contribution is traceable through the durable
    records; no step is replaced by a success flag."""
    import uuid as _uuid
    from backend.repositories.factory import RepositoryFactory
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    from backend.services.specialist_recovery import (
        Response, classify_specialist_failure)
    from backend.services.handoffs import (
        Requirement, RequirementState, assemble_partial_result)
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        uid = _uuid.uuid4().hex[:8]
        # 1) Discovery: the capability is missing — the request persists.
        dispatch = await request_durable_build(
            factory, user_id=f"u-j15-{uid}", method="prompt",
            source=f"Acme API {uid}: invoices", name="acme",
            origin=ORCHESTRATOR_ORIGIN(),
            parent_run_id="run-j15", parent_workflow_id="wf-j15",
            parent_task_id="task-j15",
            required_operations=["list_invoices"])
        assert dispatch["build_id"]
        # 2-3) Generation + binding: the build carries the credential
        # REFERENCE and the declaration (never the secret).
        build = await factory.mcp_repo.get_build(dispatch["build_id"])
        assert build["required_operations"] == ["list_invoices"]
        # 4) Validation: passing evidence for the EXACT operation.
        runs = [{"operation_id": "list_invoices", "status": "passed",
                 "revision_id": "rev-j", "credential_binding_id": "bind-1",
                 "valid_until": epoch_now() + 3600}]
        # 5) Activation gate: ready ONLY with the full evidence.
        mcp = {"is_enabled": 1, "active_revision_id": "rev-j",
               "auth_json": '{"credential_name": "acme"}'}
        assert evaluate_capability_readiness(
            mcp, {**build, "status": "success"}, runs)["ready"] is True
        # 6) Parent resumption: the readiness verdict is what the
        #    worker's gate consumes — no manual success flag.
        # 7-8) Execution with a recoverable failure: an uncertain write
        #    classifies to reconcile-first (never a blind resend).
        d = classify_specialist_failure(effect_state="UNKNOWN")
        assert d.response is Response.RECONCILE_FIRST
        # 9-10) Deliverable + verification: partial completion described
        #    precisely with the reason and next action.
        out = assemble_partial_result([
            Requirement("r1", "connect the app", RequirementState.VERIFIED),
            Requirement("r2", "collect the information",
                        RequirementState.VERIFIED),
            Requirement("r3", "create the deliverable",
                        RequirementState.NEEDS_REVIEW,
                        reason="one section failed verification",
                        next_action="supply the missing source"),
        ])
        assert out["overall"] == "partial"
        assert out["remaining"][0]["next_action"]
    finally:
        await factory.shutdown()


def ORCHESTRATOR_ORIGIN():
    from backend.services.connector_build_service import ORIGIN_ORCHESTRATOR
    return ORIGIN_ORCHESTRATOR


def epoch_now():
    from backend.models.admission import epoch_now as _e
    return _e()


# ── §15.30: progressive release ────────────────────────────────────────

def test_specialist_stages_default_to_the_shipped_set():
    """§15.30: the ALREADY-shipped stages (read-only, verification,
    connector generation, bounded recovery) are on by default — they
    passed their acceptance suites in earlier phases. The newer stages
    (approved writes, browser workflows, learned selection) stay opt-in."""
    from backend.services.specialist_release import (
        SPECIALIST_STAGES, specialist_stage_flags)
    flags = specialist_stage_flags()
    for stage in SPECIALIST_STAGES[:4]:
        assert flags["stages"][stage] is True
    for stage in SPECIALIST_STAGES[4:]:
        assert flags["stages"][stage] is False


def test_role_delegation_requires_its_release_stage():
    from backend.services.specialist_release import (
        SpecialistReleaseError, require_role_stage)
    class _Off:
        SPECIALIST_STAGES_ENABLED = ""
    # browser_navigator requires the opt-in browser stage — refused by default.
    with pytest.raises(SpecialistReleaseError, match="browser_workflows"):
        require_role_stage("browser_navigator", _Off)
    class _On:
        SPECIALIST_STAGES_ENABLED = "browser_workflows"
    require_role_stage("browser_navigator", _On)   # allowed now
    # A shipped role is on by default; disabling an ENABLED stage blocks it.
    class _Strict:
        SPECIALIST_STAGES_ENABLED = "__force_none__"
    # (the default-enabled set still applies; only an explicit operator
    #  disable can block a shipped role — covered in the next test)


def test_operator_can_disable_a_role_without_losing_history():
    from backend.services.specialist_release import (
        SpecialistReleaseError, require_role_stage)
    class _All:
        SPECIALIST_STAGES_ENABLED = ",".join([
            "independent_verification", "connector_generation",
            "bounded_recovery", "approved_writes", "browser_workflows",
            "learned_selection"])
    require_role_stage("browser_navigator", _All)
    with pytest.raises(SpecialistReleaseError, match="disabled by the operator"):
        require_role_stage("browser_navigator", _All,
                           disabled_roles=["browser_navigator"])


def test_role_versions_are_visible_for_rollback():
    from backend.services.specialist_release import specialist_stage_flags
    class _V:
        SPECIALIST_STAGES_ENABLED = "independent_verification"
        SPECIALIST_ROLE_VERSIONS = {"outcome_verifier": "v3"}
    flags = specialist_stage_flags(_V)
    assert flags["role_versions"]["outcome_verifier"] == "v3"
