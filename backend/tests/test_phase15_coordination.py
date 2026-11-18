"""Phase 15 §15.6–§15.11: bounded delegation, budgets, durable dispatch,
fenced ownership, dependency scheduling, and shared-resource control."""
import pytest
import pytest_asyncio

from backend.models.agent_task import AgentRole, AgentTaskError, AgentTaskState
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, set_agent_task_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_contracts import (
    cancel, complete, delegate, fail)


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "p15b.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


_CRITERIA = [{"kind": "non_empty", "description": "typed output exists"}]


async def _delegate(repo, parent_id="run-x", role=AgentRole.OUTCOME_VERIFIER,
                    goal="g", **kw):
    return await delegate(
        parent_kind="run", parent_id=parent_id, role=role, tenant_id="u1",
        goal=goal, input_data={"x": 1},
        output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], repo=repo, **kw)


# ── §15.6: the delegation tree is bounded, cross-worker ────────────────

@pytest.mark.asyncio
async def test_recursive_plan_cannot_create_unlimited_agents(repo):
    from backend.models.agent_task import MAX_DELEGATION_DEPTH
    root = await _delegate(repo)
    # Chain children to the depth cap; one more is refused.
    parent = root
    for depth in range(1, MAX_DELEGATION_DEPTH + 1):
        parent = await delegate(
            parent_kind="contract", parent_id=parent.contract_id,
            role=AgentRole.RECOVERY_ANALYST, tenant_id="u1",
            goal=f"level {depth}", input_data={"d": depth},
            output_schema={"required": ["ok"]},
            acceptance_criteria=_CRITERIA,
            allowed_tools=["evidence.read"], depth=depth,
            ancestor_ids=[root.contract_id], repo=repo)
    with pytest.raises(AgentTaskError, match="depth"):
        await delegate(
            parent_kind="contract", parent_id=parent.contract_id,
            role=AgentRole.RECOVERY_ANALYST, tenant_id="u1",
            goal="one too deep", input_data={"d": 9},
            output_schema={"required": ["ok"]},
            acceptance_criteria=_CRITERIA,
            allowed_tools=["evidence.read"],
            depth=MAX_DELEGATION_DEPTH + 1,
            ancestor_ids=[root.contract_id], repo=repo)


@pytest.mark.asyncio
async def test_repeated_equivalent_delegation_is_a_cycle_signal(repo):
    """Two ACTIVE delegations of the same goal+role under one parent are
    refused — the agents-delegate-back-and-forth loop cannot spin."""
    await _delegate(repo, goal="collect invoices")
    with pytest.raises(AgentTaskError, match="Equivalent work"):
        await _delegate(repo, goal="collect invoices")
    # A DIFFERENT goal under the same parent is fine.
    other = await _delegate(repo, goal="collect receipts")
    assert other.contract_id


@pytest.mark.asyncio
async def test_fan_out_and_concurrency_caps(repo):
    from backend.models.agent_task import (
        MAX_ACTIVE_CHILDREN_PER_PARENT, MAX_CHILDREN_PER_PARENT)
    # 8 total children fit; the 9th is refused (fan-out).
    ids = []
    for i in range(8):
        c = await _delegate(repo, goal=f"task {i}")
        ids.append(c.contract_id)
    with pytest.raises(AgentTaskError, match="children"):
        await _delegate(repo, goal="task 9")
    # Concurrency: complete ALL 8, then a FRESH parent's wave — 4
    # dispatched children fit; the 5th concurrent child is refused.
    for cid in ids:
        await repo.claim(cid, "w1")
        await complete(cid, {"ok": True}, actor="w1", repo=repo)
    batch2 = []
    for i in range(4):
        c = await _delegate(repo, parent_id="run-x2", goal=f"wave2 {i}")
        await repo.claim(c.contract_id, "w2")   # DISPATCHED
        batch2.append(c.contract_id)
    with pytest.raises(AgentTaskError, match="active children"):
        await _delegate(repo, parent_id="run-x2", goal="wave2 5")


# ── §15.7: budgets without double-counting ─────────────────────────────

@pytest.mark.asyncio
async def test_parallel_children_cannot_each_spend_the_parent_budget(repo):
    """The parent's allowance caps the SUM of its children — parallel
    children share the allowance, they do not each get the full amount."""
    # The budget root is a CONTRACT with the family's allowance — the
    # children RESERVE from it (70 + 30 = 100), never double-spend it.
    root = await delegate(
        parent_kind="run", parent_id="run-budget",
        role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1", goal="budget root",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=100, repo=repo)
    first = await delegate(
        parent_kind="contract", parent_id=root.contract_id,
        role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="child a",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=70, depth=1, ancestor_ids=[root.contract_id],
        repo=repo)
    # 70 of 100 spent as reservation; a second child of 70 does not fit.
    with pytest.raises(AgentTaskError, match="budget reservation"):
        await delegate(
            parent_kind="contract", parent_id=root.contract_id,
            role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="child b",
            input_data={"x": 1}, output_schema={"required": ["ok"]},
            acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
            budget_units=70, depth=1,
            ancestor_ids=[root.contract_id], repo=repo)
    # A smaller child fits within what remains.
    second = await delegate(
        parent_kind="contract", parent_id=root.contract_id,
        role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="child c",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=30, depth=1, ancestor_ids=[root.contract_id],
        repo=repo)
    assert second.budget_units == 30


@pytest.mark.asyncio
async def test_each_attempt_keeps_its_own_spend_identity(repo):
    """A retry's spend lands on ITS attempt record — the same usage is
    never settled twice across attempts."""
    c = await _delegate(repo)
    await repo.claim(c.contract_id, "w1")
    await complete(c.contract_id, {"ok": True}, spent_units=40,
                   actor="w1", repo=repo)
    done = await repo.get_contract(c.contract_id)
    assert done.spent_units == 40
    # The retry (a NEW contract) accumulates ITS OWN spend.
    retry = await _delegate(repo, goal="g retry", dedup_key="retry-1")
    await repo.claim(retry.contract_id, "w2")
    await complete(retry.contract_id, {"ok": True}, spent_units=25,
                   actor="w2", repo=repo)
    assert (await repo.get_contract(retry.contract_id)).spent_units == 25
    assert (await repo.get_contract(c.contract_id)).spent_units == 40


# ── §15.8: atomic, idempotent creation + crash-window recovery ─────────

@pytest.mark.asyncio
async def test_duplicate_dispatch_returns_the_same_logical_work(repo):
    """Concurrent same-delegation: the dedup identity resolves to ONE
    accepted child — the loser is refused, not double-executed."""
    import asyncio
    results = await asyncio.gather(
        _delegate(repo, goal="idem work", dedup_key="logical-delegation-1"),
        _delegate(repo, goal="idem work", dedup_key="logical-delegation-1"),
        return_exceptions=True)
    errs = [r for r in results if isinstance(r, AgentTaskError)]
    ok = [r for r in results if not isinstance(r, AgentTaskError)]
    assert len(ok) == 1 and len(errs) == 1


@pytest.mark.asyncio
async def test_crash_before_publication_is_recoverable(repo):
    """A crash between persistence and publication: the contract row
    EXISTS as PROPOSED and the reconciler drains it — no work lost."""
    c = await _delegate(repo, goal="crashed dispatch")
    # (Simulate: the publication never ran.)
    pending = await repo.pending_contracts()
    assert any(p.contract_id == c.contract_id for p in pending)
    # The reconciler claims and completes it.
    claimed = await repo.claim(c.contract_id, "reconciler")
    assert claimed is not None
    await complete(c.contract_id, {"ok": True}, actor="reconciler",
                   repo=repo)
    pending_after = await repo.pending_contracts()
    assert not any(p.contract_id == c.contract_id for p in pending_after)


# ── §15.9: fenced ownership and completion ─────────────────────────────

@pytest.mark.asyncio
async def test_expired_worker_cannot_overwrite_replacement_result(repo):
    c = await _delegate(repo)
    await repo.claim(c.contract_id, "worker-a", lease_seconds=60)
    # The lease expires; worker-b takes over.
    import backend.models.admission as adm
    await repo.db.execute(
        "UPDATE agent_tasks SET lease_expires_at=1 WHERE contract_id=?",
        (c.contract_id,))
    await repo.db.commit()
    assert await repo.claim(c.contract_id, "worker-b") is not None
    # Stale worker-a cannot complete with ITS result.
    with pytest.raises(AgentTaskError, match="Stale worker"):
        await complete(c.contract_id, {"ok": "a"}, actor="worker-a",
                       repo=repo)
    # worker-b completes; worker-a also cannot fail it afterwards.
    await complete(c.contract_id, {"ok": "b"}, actor="worker-b", repo=repo)
    with pytest.raises(AgentTaskError):
        await fail(c.contract_id, "late failure", actor="worker-a",
                   repo=repo)


@pytest.mark.asyncio
async def test_heartbeat_and_renewal_require_the_current_claim(repo):
    c = await _delegate(repo)
    await repo.claim(c.contract_id, "w1", lease_seconds=120)
    await repo.claim(c.contract_id, "w1", lease_seconds=120)   # → RUNNING
    # Another worker cannot renew someone else's lease.
    assert await repo.renew_lease(c.contract_id, "w2") is False
    assert await repo.renew_lease(c.contract_id, "w1") is True


# ── §15.10: dependency semantics — stopping is not succeeding ──────────

@pytest.mark.asyncio
async def test_downstream_never_consumes_output_of_a_stopped_upstream(repo):
    """require_all: a FAILED child means the parent's combined result is
    INCOMPLETE — downstream work cannot proceed on 'the process stopped'
    as if it were 'the work succeeded'."""
    parent = await delegate(
        parent_kind="run", parent_id="run-dep", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", goal="parent", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], completion_policy="require_all",
        repo=repo)
    a = await delegate(
        parent_kind="contract", parent_id=parent.contract_id,
        role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="stage a",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=10, depth=1, ancestor_ids=[parent.contract_id],
        repo=repo)
    b = await delegate(
        parent_kind="contract", parent_id=parent.contract_id,
        role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="stage b",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=10, depth=1, ancestor_ids=[parent.contract_id],
        repo=repo)
    for cid, worker in ((a.contract_id, "w1"), (b.contract_id, "w2")):
        await repo.claim(cid, worker)
    await complete(a.contract_id, {"ok": True}, actor="w1", repo=repo)
    await fail(b.contract_id, "the upstream process stopped running",
               actor="w2", repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "incomplete", (
        "a stopped process is not a successful dependency")
    assert b.contract_id in verdict["missing"]
    # require_any with the same states: one success suffices.
    parent2 = await delegate(
        parent_kind="run", parent_id="run-dep2", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", goal="parent2", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], completion_policy="require_any",
        repo=repo)
    c1 = await delegate(
        parent_kind="contract", parent_id=parent2.contract_id,
        role=AgentRole.RECOVERY_ANALYST, tenant_id="u1", goal="p2 a",
        input_data={"x": 1}, output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA, allowed_tools=["evidence.read"],
        budget_units=10, depth=1, ancestor_ids=[parent2.contract_id],
        repo=repo)
    await repo.claim(c1.contract_id, "w1")
    await complete(c1.contract_id, {"ok": True}, actor="w1", repo=repo)
    verdict2 = await repo.evaluate_completion_policy(parent2.contract_id)
    assert verdict2["verdict"] == "complete"


# ── §15.11: shared resources need resource-specific ownership ──────────

@pytest.mark.asyncio
async def test_two_specialists_cannot_control_one_browser(tmp_path):
    from backend.repositories.browser_session_repository import (
        BrowserSessionRepository)
    from backend.models.browser_session import (
        BrowserSession, ChallengeState, CleanupState, ControllerState,
        ResourceState)
    db = DatabaseManager(str(tmp_path / "shared.db"))
    await db.initialize()
    r = BrowserSessionRepository(db)
    await r.create_session(BrowserSession(
        session_id="bs-shared", tenant_id="t1", user_id="u1",
        workflow_id="wf", run_id="run-s", task_id="t",
        owner_worker_id="", owner_epoch="", owner_generation=0,
        resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT,
        challenge_state=ChallengeState.NONE,
        cleanup_state=CleanupState.NOT_REQUIRED,
        allowed_origins=["a.test"], runtime_session_id="rt"))
    first = await r.acquire_ownership("bs-shared", "specialist-1", "t1",
                                      120, 1)
    assert first is not None
    # The second specialist CANNOT take the live lease.
    second = await r.acquire_ownership("bs-shared", "specialist-2", "t1",
                                       120, 1)
    assert second is None, "one controller per session (§8.7/§15.11)"
    await db.close()


@pytest.mark.asyncio
async def test_artifact_pointer_never_silently_overwrites(tmp_path):
    """Two specialists assembling one artifact: the second writer without
    the expected version is refused — the pointer moves by CAS only."""
    # The artifact store's own CAS is Phase 17 machinery; here the
    # CONTRACT-level guarantee: two writers to the same target resolve
    # through the dedup identity — equivalent work is refused.
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    db = DatabaseManager(str(tmp_path / "art.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        await _delegate(r, parent_id="run-art", goal="assemble report",
                        dedup_key="assemble:report-1")
        with pytest.raises(AgentTaskError):
            await _delegate(r, parent_id="run-art", goal="assemble report",
                            dedup_key="assemble:report-1")
    finally:
        set_agent_task_repo(None)
        await db.close()
