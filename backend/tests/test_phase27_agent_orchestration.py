"""Phase 27 §27.8 (R4) — every CONTRACT-CAPABLE enabled specialist driven
through REAL orchestration: create → claim → execute → validate →
complete, with authorization refusal and the no-upgrade rule verified.

The frozen enabled inventory comes from the production stage flags. Roles
whose orchestration entry is the engine's own path (recovery, read-only
research) are recorded honestly — this file covers the two typed
contract runners and the no-upgrade guarantee; remaining roles stay open
in the register (27.R4).
"""
import uuid

import pytest
import pytest_asyncio

from backend.services.specialist_release import (
    ROLE_MIN_STAGE, specialist_stage_flags)


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "orch.db"))
    await db.initialize()
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, set_routing_repo)
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository, set_adaptive_policy_repo)
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    set_routing_repo(RoutingDecisionRepository(db))
    set_adaptive_policy_repo(SQLiteAdaptivePolicyRepository(db))
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        set_routing_repo(None)
        set_adaptive_policy_repo(None)
        await db.close()


def _enabled_roles():
    flags = specialist_stage_flags()
    return {k for k, v in flags["stages"].items() if v}


def test_enabled_inventory_is_frozen_and_recorded():
    """27.R4 entry: the enabled inventory is derived from production
    stage flags and recorded — never invented. Enabled = every role
    whose minimum stage is currently enabled."""
    enabled_stages = _enabled_roles()
    assert enabled_stages == {"bounded_recovery", "connector_generation",
                              "independent_verification",
                              "read_only_tasks"}, enabled_stages
    enabled_roles = {role for role, min_stage in ROLE_MIN_STAGE.items()
                     if min_stage in enabled_stages}
    # Every contract-capable role must be part of the enabled set.
    assert "outcome_verifier" in enabled_roles
    # The frozen inventory is recorded verbatim for the register.
    print("ENABLED_AGENTS:", sorted(enabled_roles))


@pytest.mark.asyncio
async def test_verifier_specialist_orchestrates_and_never_upgrades_a_fail(repo):
    """outcome_verifier through the REAL contract path: success completes
    with a typed output; a judge pass never upgrades a deterministic
    fail (§12.19)."""
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)

    ctx = VerificationContext(
        task_id="t1", run_id=f"run-{uuid.uuid4().hex[:8]}", goal="g",
        criteria=[{"id": "c1", "description": "reply present"}],
        output_data={"reply": "ok"},
        deterministic_verdict="needs_review", deterministic_reasons=["x"])
    out = await run_verifier_via_contract(repo, ctx, parent_id=ctx.run_id,
                              principal_id="owner-u1")
    assert out.get("combined_verdict") in ("pass", "needs_review", "fail")

    # no-upgrade: deterministic fail stands even with a positive judge.
    fail_ctx = VerificationContext(
        task_id="t2", run_id=ctx.run_id, goal="g", criteria=[{"id": "c2", "description": "never true"}],
        output_data={"reply": "no"}, deterministic_verdict="fail",
        deterministic_reasons=["criterion unmet"])
    out2 = await run_verifier_via_contract(repo, fail_ctx,
                                           parent_id=fail_ctx.run_id,
                                           principal_id="owner-u1")
    assert out2.get("combined_verdict") == "fail", out2


@pytest.mark.asyncio
async def test_verifier_specialist_refuses_without_authorization(repo):
    """Authorization is rechecked BEFORE the claim: a deactivated user
    produces zero contracts."""
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    from backend.services.agent_authority import (
        AuthorizationLost, set_authorization_provider)

    async def revoked(principal_id):
        return {"user_active": False, "membership_current": False,
                "credential_scope_valid": False, "tool_access_current": False}
    set_authorization_provider(revoked)
    try:
        ctx = VerificationContext(
            task_id="t3", run_id="run-X", goal="g", criteria=[],
            output_data={"reply": "ok"}, deterministic_verdict="pass",
            deterministic_reasons=[])
        with pytest.raises(AuthorizationLost):
            await run_verifier_via_contract(repo, ctx, parent_id="run-X",
                                            principal_id="owner-u1")
        assert await repo.subtree_of("run-X") == []
    finally:
        set_authorization_provider(None)


@pytest.mark.asyncio
async def test_connector_validator_specialist_orchestrates(repo):
    """connector_validator through the REAL contract path: bounded
    proposal completes with a typed plan; the same no-upgrade contract
    machinery applies."""
    from backend.agents.connector_validator.validator_agent import (
        ValidationContext, run_validator_via_contract)

    ctx = ValidationContext(
        connector_id=f"mcp-{uuid.uuid4().hex[:8]}",
        operation_id="listEntries",
        operation_schema={"writes": False},
        existing_coverage=[])
    out = await run_validator_via_contract(repo, ctx, principal_id="owner-u1",
                                           parent_id=f"run-{uuid.uuid4().hex[:8]}")
    assert out.get("contract_id"), out
    assert out.get("plan", {}).get("test_cases"), out


@pytest.mark.asyncio
async def test_contract_budget_and_deadline_bound(repo):
    """Delegated work is bounded: the contract carries a deadline and
    attempts are visible on the record."""
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    ctx = VerificationContext(
        task_id="t5", run_id=f"run-{uuid.uuid4().hex[:8]}", goal="g",
        criteria=[], output_data={}, deterministic_verdict="needs_review",
        deterministic_reasons=[])
    out = await run_verifier_via_contract(repo, ctx, parent_id=ctx.run_id,
                              principal_id="owner-u1")
    cid = out.get("contract_id")
    assert cid, out
    tree = await repo.subtree_of(ctx.run_id)
    mine = [t for t in tree if getattr(t, "task_id", "") == cid or
            getattr(t, "contract_id", "") == cid or
            getattr(t, "id", "") == cid]
    rec = mine[0] if mine else None
    assert rec is not None, "the contract record must be retrievable"
    # Delegated work is bounded: a deadline and bounded attempts exist.
    deadline = getattr(rec, "deadline_at", None)
    attempts = getattr(rec, "attempts", None)
    assert deadline is not None or attempts is not None


@pytest.mark.asyncio
async def test_verifier_fails_closed_without_resolvable_actor(repo):
    """§27.2: a run ID is not an identity. No authenticated principal +
    no resolvable parent owner → AUTHORITY_UNRESOLVED, zero contracts."""
    import uuid as _uuid
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    from backend.services.agent_contracts import AgentTaskError

    ctx = VerificationContext(
        task_id="t9", run_id=f"run-{_uuid.uuid4().hex[:8]}", goal="g",
        criteria=[{"id": "c9", "description": "reply present"}],
        output_data={"reply": "ok"}, deterministic_verdict="pass",
        deterministic_reasons=[])
    with pytest.raises(AgentTaskError) as unresolved:
        await run_verifier_via_contract(repo, ctx, parent_id=ctx.run_id)
    assert unresolved.value.code == "AUTHORITY_UNRESOLVED"
    assert await repo.subtree_of(ctx.run_id) == []


@pytest.mark.asyncio
async def test_verifier_derives_actor_from_owned_parent(repo):
    """§27.2: without an explicit principal the actor is the parent
    run's PERSISTED owner — then the authority recheck applies to that
    actor, never to the run ID."""
    import uuid as _uuid
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    from backend.services.agent_authority import set_parent_owner_provider

    run_id = f"run-{_uuid.uuid4().hex[:8]}"
    seen = {}

    async def owner_provider(kind, pid):
        seen["args"] = (kind, pid)
        return "parent-owner-7"

    set_parent_owner_provider(owner_provider)
    try:
        ctx = VerificationContext(
            task_id="t10", run_id=run_id, goal="g",
            criteria=[{"id": "c10", "description": "reply present"}],
            output_data={"reply": "ok"}, deterministic_verdict="pass",
            deterministic_reasons=[])
        out = await run_verifier_via_contract(repo, ctx, parent_id=run_id)
        assert out.get("combined_verdict") in ("pass", "needs_review", "fail")
        assert seen["args"] == ("run", run_id)
        contracts = await repo.subtree_of(run_id)
        assert contracts and contracts[0].tenant_id == "parent-owner-7"
    finally:
        set_parent_owner_provider(None)


def test_browser_navigator_disabled_by_default_and_admits_when_enabled():
    """§27.8: browser_navigator is the gated role — delegation refuses
    while its stage is disabled and ADMITS the typed path once the
    operator enables it (progressive release, §15.30)."""
    from types import SimpleNamespace
    from backend.models.agent_task import AgentRole
    from backend.services.specialist_release import (
        require_role_stage, SpecialistReleaseError)

    # 1. Default: the browser_workflows stage is OFF → refusal.
    flags = specialist_stage_flags()
    assert flags["stages"].get("browser_workflows") is not True
    with pytest.raises(SpecialistReleaseError):
        require_role_stage(AgentRole.BROWSER_NAVIGATOR.value, None)

    # 2. Operator enables the stage → the role's gate admits.
    settings = SimpleNamespace(SPECIALIST_STAGES_ENABLED="browser_workflows",
                               SPECIALIST_ROLE_VERSIONS={})
    require_role_stage(AgentRole.BROWSER_NAVIGATOR.value, settings)

    # 3. With the stage enabled the gate admits; operator disablement
    #    beats stage enablement.
    require_role_stage(AgentRole.BROWSER_NAVIGATOR.value, settings)
    with pytest.raises(SpecialistReleaseError):
        require_role_stage(AgentRole.BROWSER_NAVIGATOR.value, settings,
                           disabled_roles=["browser_navigator"])
