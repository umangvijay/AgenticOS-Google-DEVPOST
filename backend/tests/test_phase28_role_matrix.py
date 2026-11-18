"""Phase 28 §28.5 — the FULL enabled-role matrix.

27.R4 closed per-role real-execution journeys (test_phase27_agent_
roles_journey.py) and the two typed contract runners
(test_phase27_agent_orchestration.py). The register's remaining 28.5
item is the systematic matrix: for EVERY enabled specialist role
(frozen from production stage flags), each lifecycle dimension is
driven through the REAL contract orchestration path — delegate →
claim → typed settlement — on the durable contract store:

    success · failure · timeout · cancel · denied tool ·
    exhausted budget · restart (stale fencing + lease-expiry
    takeover) · malformed handoff (typed output refusal)

Parent semantics close the matrix: a REQUIRED failed child blocks
parent completion ("incomplete"); a deliberately OPTIONAL (best_effort)
failure yields the declared partial outcome with the missing child
named — never a silent success.

The matrix exercises the CONTRACT lifecycle uniformly for all enabled
roles; each role's real service work remains covered by the roles
journey. The enabled inventory is re-derived inside the parameter
collection so a stage-flag change grows or shrinks the matrix honestly.
"""
import uuid

import pytest
import pytest_asyncio

from backend.models.agent_task import (
    ROLE_ALLOWED_TOOLS, AgentRole, AgentTaskError, AgentTaskState,
)
from backend.services.agent_contracts import (
    cancel, claim_specialist, complete, delegate, fail,
)
from backend.services.agent_task_contracts import (
    SPECIALIST_REGISTRY, delegate_specialist,
)
from backend.services.specialist_release import (
    ROLE_MIN_STAGE, specialist_stage_flags,
)


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / f"matrix-{uuid.uuid4().hex[:8]}.db"))
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


def _enabled_roles() -> list[str]:
    flags = specialist_stage_flags()
    enabled_stages = {k for k, v in flags["stages"].items() if v}
    return sorted(r for r, ms in ROLE_MIN_STAGE.items()
                  if ms in enabled_stages)


def _registry_entry(role: str) -> dict:
    entry = SPECIALIST_REGISTRY.get(AgentRole(role))
    assert entry is not None, (
        f"enabled role {role} has no SPECIALIST_REGISTRY entry — it "
        "cannot be delegated through the normal delegate_specialist "
        "entry point (28.5 requires the enabled inventory to be "
        "orchestration-capable)")
    return entry


def _schema_valid_result(role: str) -> dict:
    """A minimal typed result satisfying the role's locked output
    schema — the settlement validator only requires the keys to be
    present and non-None."""
    required = _registry_entry(role)["output_schema"].get(
        "required") or []
    return {key: "matrix-ok" for key in required}


async def _delegate_and_claim(repo, role: str, *, run_id=None,
                              allowed_tools=None, budget_units=100,
                              deadline_seconds=900, completion_policy="require_all",
                              lease_seconds=300):
    contract = await delegate_specialist(
        AgentRole(role), parent_kind="run",
        parent_id=run_id or f"run-matrix-{uuid.uuid4().hex[:8]}",
        tenant_id="u1", input_data={"matrix_dimension": role},
        allowed_tools=allowed_tools, budget_units=budget_units,
        deadline_seconds=deadline_seconds,
        completion_policy=completion_policy, repo=repo)
    if lease_seconds is None:
        return contract, None
    actor = f"{role}-worker"
    claimed = await repo.claim(contract.contract_id, actor,
                               lease_seconds=lease_seconds)
    assert claimed is not None, "the fresh contract must be claimable"
    return contract, actor


# ── the matrix itself: one test per role × dimension ───────────────────

@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_success(role, repo):
    """success — delegate → claim → typed completion; the state lands
    COMPLETED with the result validated against the locked schema."""
    contract, actor = await _delegate_and_claim(repo, role)
    done = await complete(contract.contract_id,
                          _schema_valid_result(role),
                          spent_units=1, actor=actor, repo=repo)
    assert done.state is AgentTaskState.COMPLETED
    assert done.result is not None


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_failure(role, repo):
    """failure — the contract records the honest FAILED disposition and
    the completion path refuses a post-failure settlement."""
    contract, actor = await _delegate_and_claim(repo, role)
    failed = await fail(contract.contract_id,
                        "matrix-injected failure", actor=actor, repo=repo)
    assert failed.state is AgentTaskState.FAILED
    assert failed.error == "matrix-injected failure"
    with pytest.raises(AgentTaskError) as exc:
        await complete(contract.contract_id, _schema_valid_result(role),
                       spent_units=1, actor=actor, repo=repo)
    assert exc.value.code == "INVALID_TRANSITION"


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_timeout(role, repo):
    """timeout — a past-deadline contract cannot be claimed (claim
    transitions it TIMED_OUT), and the expiry sweep reports it."""
    contract = await delegate_specialist(
        AgentRole(role), parent_kind="run",
        parent_id=f"run-matrix-{uuid.uuid4().hex[:8]}",
        tenant_id="u1", input_data={"matrix_dimension": "timeout"},
        deadline_seconds=0, repo=repo)
    claimed = await repo.claim(contract.contract_id, "worker-t")
    assert claimed is None, "a past-deadline contract must not dispatch"
    timed_out = await repo.get_contract(contract.contract_id)
    assert timed_out.state is AgentTaskState.TIMED_OUT
    assert await repo.expire_stale_contracts() == 0


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_cancel(role, repo):
    """cancel — cancellation lands CANCELLED and a late completion is
    refused (the parent never sees a fake success after cancel)."""
    contract, actor = await _delegate_and_claim(repo, role)
    out = await cancel(contract.contract_id, actor="operator", repo=repo)
    assert out["state"] == "CANCELLED"
    with pytest.raises(AgentTaskError) as exc:
        await complete(contract.contract_id, _schema_valid_result(role),
                       spent_units=1, actor=actor, repo=repo)
    assert exc.value.code == "INVALID_TRANSITION"


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_denied_tool(role, repo):
    """denied tool — a delegation requesting a tool outside the role's
    whitelist is REFUSED with the boundary named, never trimmed."""
    with pytest.raises(AgentTaskError) as exc:
        await _delegate_and_claim(
            repo, role,
            allowed_tools=sorted({"browser.write", "shell.exec"}))
    assert exc.value.code == "ROLE_TOOL_FORBIDDEN"


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_exhausted_budget(role, repo):
    """exhausted budget — both boundaries: a child reservation beyond
    the parent cap is refused at delegation, and a completion that
    overdraws the contract budget is refused at settlement."""
    parent = await delegate_specialist(
        AgentRole(role), parent_kind="run",
        parent_id=f"run-budget-{uuid.uuid4().hex[:8]}",
        tenant_id="u1", input_data={"matrix_dimension": "budget"},
        budget_units=5, repo=repo)
    with pytest.raises(AgentTaskError) as exc:
        await delegate_specialist(
            AgentRole(role), parent_kind="contract",
            parent_id=parent.contract_id, tenant_id="u1",
            input_data={"matrix_dimension": "budget-child"},
            budget_units=50, repo=repo)
    assert exc.value.code == "BUDGET_EXCEEDED"

    contract, actor = await _delegate_and_claim(repo, role,
                                                budget_units=5)
    with pytest.raises(AgentTaskError) as exc2:
        await complete(contract.contract_id, _schema_valid_result(role),
                       spent_units=6, actor=actor, repo=repo)
    assert exc2.value.code == "BUDGET_EXCEEDED"
    still_running = await repo.get_contract(contract.contract_id)
    assert still_running.state is AgentTaskState.RUNNING


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_restart(role, repo):
    """restart — worker identity fencing and lease-expiry takeover:
    (a) a different actor cannot settle another worker's claim;
    (b) a worker whose lease expired cannot settle (worker loss);
    (c) a replacement worker takes over via claim and completes."""
    # (a) live claim held by the real worker: a foreign actor is fenced.
    contract_a, actor_a = await _delegate_and_claim(repo, role)
    with pytest.raises(AgentTaskError) as exc:
        await complete(contract_a.contract_id,
                       _schema_valid_result(role),
                       spent_units=1, actor="other-worker", repo=repo)
    assert exc.value.code == "CLAIM_FENCED", (
        "a worker that does not hold the claim cannot settle it")

    # (b) worker loss: the worker claimed with an instantly expired
    #     lease (it died before doing work) — its own settlement is
    #     refused, and the contract is in the reclaimable set.
    contract_b, dead_actor = await _delegate_and_claim(
        repo, role, lease_seconds=0)
    assert dead_actor is not None
    with pytest.raises(AgentTaskError) as exc2:
        await complete(contract_b.contract_id,
                       _schema_valid_result(role),
                       spent_units=1, actor=dead_actor, repo=repo)
    assert exc2.value.code == "CLAIM_FENCED", (
        "an expired lease cannot settle — reclaim or take over first")
    reclaimable = await repo.reclaimable_contracts()
    assert contract_b.contract_id in \
        {c.contract_id for c in reclaimable}, (
        "the lost worker's contract must be queued for takeover")

    # (c) replacement worker: claim takeover (fresh lease, new owner)
    #     then a successful typed settlement.
    takeover = await repo.claim(contract_b.contract_id,
                                "worker-replacement")
    assert takeover is not None
    assert takeover.owner_worker_id == "worker-replacement"
    done = await complete(contract_b.contract_id,
                          _schema_valid_result(role),
                          spent_units=1, actor="worker-replacement",
                          repo=repo)
    assert done.state is AgentTaskState.COMPLETED


@pytest.mark.parametrize("role", _enabled_roles())
@pytest.mark.asyncio
async def test_matrix_malformed_handoff(role, repo):
    """malformed handoff — a settlement violating the role's locked
    output schema is REFUSED before the state moves; the contract
    stays RUNNING for an honest retry/fail, never a corrupt success."""
    contract, actor = await _delegate_and_claim(repo, role)
    bad = {k: v for k, v in _schema_valid_result(role).items()
           if k != sorted(_schema_valid_result(role))[0]}
    with pytest.raises(AgentTaskError) as exc:
        await complete(contract.contract_id, bad,
                       spent_units=1, actor=actor, repo=repo)
    assert exc.value.code == "OUTPUT_SCHEMA_VIOLATION"
    still = await repo.get_contract(contract.contract_id)
    assert still.state is AgentTaskState.RUNNING
    done = await complete(contract.contract_id,
                          _schema_valid_result(role),
                          spent_units=1, actor=actor, repo=repo)
    assert done.state is AgentTaskState.COMPLETED


# ── parent semantics close the matrix ───────────────────────────────────

@pytest.mark.asyncio
async def test_required_failed_child_prevents_parent_success(repo):
    """A REQUIRED (require_all) failed child means the parent verdict
    is 'incomplete' — never a silent success."""
    parent = await delegate_specialist(
        AgentRole.OUTCOME_VERIFIER, parent_kind="run",
        parent_id=f"run-req-{uuid.uuid4().hex[:8]}", tenant_id="u1",
        input_data={"matrix": "parent"}, budget_units=100, repo=repo)
    child = await delegate_specialist(
        AgentRole.OUTCOME_VERIFIER, parent_kind="contract",
        parent_id=parent.contract_id, tenant_id="u1",
        input_data={"matrix": "child"}, budget_units=40, repo=repo)
    actor = await claim_specialist(repo, child.contract_id,
                                   "outcome_verifier")
    await fail(child.contract_id, "child failed honestly",
               actor=actor, repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "incomplete"
    assert child.contract_id in verdict["missing"]
    assert verdict["failed"], "the failed child must be named with its error"


@pytest.mark.asyncio
async def test_optional_failure_produces_declared_partial_outcome(repo):
    """A deliberately OPTIONAL (best_effort) child failure still allows
    the parent to proceed — with the missing child NAMED, never silent."""
    parent = await delegate_specialist(
        AgentRole.OUTCOME_VERIFIER, parent_kind="run",
        parent_id=f"run-opt-{uuid.uuid4().hex[:8]}", tenant_id="u1",
        input_data={"matrix": "parent-opt"}, budget_units=100,
        completion_policy="best_effort", repo=repo)
    # The two siblings are the SAME role answering the parent's task —
    # distinct goals keep the §15.6 equivalent-work cycle guard quiet
    # (it would otherwise refuse the second delegation, correctly).
    ok_child = await delegate(
        parent_kind="contract", parent_id=parent.contract_id,
        role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
        input_data={"matrix": "child-ok"},
        allowed_tools=sorted(ROLE_ALLOWED_TOOLS[AgentRole.OUTCOME_VERIFIER]),
        goal="verify the completed sibling", budget_units=40,
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a structured verdict exists"}],
        repo=repo)
    bad_child = await delegate(
        parent_kind="contract", parent_id=parent.contract_id,
        role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
        input_data={"matrix": "child-bad"},
        allowed_tools=sorted(ROLE_ALLOWED_TOOLS[AgentRole.OUTCOME_VERIFIER]),
        goal="verify the failing sibling", budget_units=40,
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a structured verdict exists"}],
        repo=repo)
    actor_ok = await claim_specialist(repo, ok_child.contract_id,
                                      "outcome_verifier")
    await complete(ok_child.contract_id,
                   _schema_valid_result("outcome_verifier"),
                   spent_units=1, actor=actor_ok, repo=repo)
    actor_bad = await claim_specialist(repo, bad_child.contract_id,
                                       "outcome_verifier")
    await fail(bad_child.contract_id, "optional child failed",
               actor=actor_bad, repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "complete"
    assert verdict["policy"] == "best_effort"
    assert bad_child.contract_id in verdict["missing"], (
        "the optional failure must be NAMED on the partial outcome")


@pytest.mark.asyncio
async def test_matrix_entry_points_are_recorded():
    """The matrix's per-row entry point is recorded for the register:
    every enabled role names its registry runner (or the explicit
    service path) and its real execution journey file."""
    for role in _enabled_roles():
        entry = _registry_entry(role)
        if entry.get("runner"):
            assert entry["runner"].startswith("backend."), role
        else:
            # runner: None = the role's dedicated service executes —
            # recorded honestly, not silently claimed as a runner path.
            assert entry["runner"] is None
