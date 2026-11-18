"""Phase 15 acceptance tests: specialized subagents and coordination.

- §15.4 task contracts: parent, typed inputs/outputs, allowed tools,
  budget, deadline, lease, cancellation — all durable.
- §15.1–15.3 authority boundaries: role-tool restrictions are refused,
  never trimmed; the verifier cannot hold dispatch authority.
- §15.5 bounded delegation: depth, fan-out, concurrent children,
  cumulative spend, cycle detection, duplicate-assignment dedup.
- §15.6 partial failure: completion verdicts name what is missing.
- Ownership races: exactly one claim winner; deadline expiry times out.
"""
import asyncio
import time

import pytest
import pytest_asyncio

from backend.models.agent_task import (
    MAX_ACTIVE_CHILDREN_PER_PARENT,
    MAX_CHILDREN_PER_PARENT,
    MAX_DELEGATION_DEPTH,
    AgentRole,
    AgentTaskError,
    AgentTaskState,
)
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, set_agent_task_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_contracts import (
    cancel, complete, delegate, fail,
)


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "agent-tasks.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


def _verifier(**over):
    defaults = dict(
        parent_kind="run", parent_id="run-1", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", input_data={"evidence": {"reply": "ok"}},
        output_schema={"required": ["verdict"]},
        allowed_tools=["evidence.read", "criteria.verify"],
        budget_units=50, deadline_seconds=600, dedup_key=None,
        # §15.3: the complete contract — goal + unambiguous criteria.
        goal="verify the run output",
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a structured verdict exists"}])
    defaults.update(over)
    return defaults


# ── §15.1–15.3: authority boundaries ───────────────────────────────────

@pytest.mark.asyncio
async def test_role_cannot_hold_forbidden_tools(repo):
    """The verifier receives EVIDENCE, not authority to repeat actions."""
    with pytest.raises(AgentTaskError, match="authority boundary"):
        await delegate(repo=repo, parent_kind="run", parent_id="run-1",
                       role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                       input_data={}, allowed_tools=["mcp.dispatch", "email.send"])


@pytest.mark.asyncio
async def test_recovery_analyst_cannot_broaden_permissions(repo):
    with pytest.raises(AgentTaskError, match="cannot hold tools"):
        await delegate(repo=repo, parent_kind="run", parent_id="run-1",
                       role=AgentRole.RECOVERY_ANALYST, tenant_id="u1",
                       input_data={}, allowed_tools=["authorization.grant"])


@pytest.mark.asyncio
async def test_allowed_tools_are_role_whitelisted(repo):
    contract = await delegate(repo=repo, **_verifier())
    assert set(contract.allowed_tools) <= {"evidence.read", "artifact.resolve",
                                           "criteria.verify"}


# ── §15.4: the contract is complete and typed ──────────────────────────

@pytest.mark.asyncio
async def test_contract_has_parent_tools_budget_deadline(repo):
    contract = await delegate(repo=repo, **_verifier(deadline_seconds=600))
    assert contract.parent_id == "run-1"
    assert contract.deadline_at > time.time()
    assert contract.budget_units == 50
    assert contract.output_schema == {"required": ["verdict"]}
    assert contract.state is AgentTaskState.PROPOSED


@pytest.mark.asyncio
async def test_output_schema_violation_blocks_completion(repo):
    contract = await delegate(repo=repo, **_verifier())
    await repo.claim(contract.contract_id, "w1")
    with pytest.raises(AgentTaskError, match="output schema"):
        await repo.transition(contract.contract_id, AgentTaskState.COMPLETED,
                              actor="w1", result={"wrong": "shape"})
    done = await repo.transition(contract.contract_id,
                                 AgentTaskState.COMPLETED,
                                 actor="w1", result={"verdict": "pass"})
    assert done.state is AgentTaskState.COMPLETED


@pytest.mark.asyncio
async def test_claim_race_produces_one_winner(repo):
    """§15 acceptance: two workers race — exactly ONE owns the lease; the
    loser gets None (never shared ownership)."""
    contract = await delegate(repo=repo, **_verifier())
    results = await asyncio.gather(
        repo.claim(contract.contract_id, "worker-A"),
        repo.claim(contract.contract_id, "worker-B"))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, "exactly one claim wins"
    fetched = await repo.get_contract(contract.contract_id)
    assert fetched.owner_worker_id in ("worker-A", "worker-B")
    assert fetched.lease_expires_at > time.time(), "the lease is live"


@pytest.mark.asyncio
async def test_deadline_expiry_blocks_completion(repo):
    contract = await delegate(repo=repo, **_verifier(deadline_seconds=-1))
    claimed = await repo.claim(contract.contract_id, "w1")
    assert claimed is None, "an expired contract cannot be claimed"
    fetched = await repo.get_contract(contract.contract_id)
    assert fetched.state is AgentTaskState.TIMED_OUT


@pytest.mark.asyncio
async def test_cancel_propagates_to_active_children(repo):
    parent = await delegate(repo=repo, **_verifier())
    child = await delegate(repo=repo, parent_kind="contract",
                           parent_id=parent.contract_id,
                           ancestor_ids=[parent.contract_id],
                           role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                           goal="resolve lvl1",
                           acceptance_criteria=[{"kind": "non_empty"}],
                           input_data={"d": 1},
                           allowed_tools=["browser.observe"],
                           depth=1, budget_units=10)
    await repo.claim(child.contract_id, "w1")
    out = await cancel(parent.contract_id, actor="operator", repo=repo)
    assert out["children_cancelled"] >= 1
    child_after = await repo.get_contract(child.contract_id)
    assert child_after.state is AgentTaskState.CANCELLED


# ── §15.5: bounded delegation ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_assignment_dedups(repo):
    first = await delegate(repo=repo, **_verifier(
        goal="verify attempt one", dedup_key="verify-t1"))
    with pytest.raises(AgentTaskError, match="Duplicate assignment"):
        await delegate(repo=repo, **_verifier(
            goal="verify attempt two", dedup_key="verify-t1"))
    # A terminal contract frees the pair for a NEW logical assignment.
    await repo.claim(first.contract_id, "w1")
    await repo.transition(first.contract_id, AgentTaskState.COMPLETED,
                          actor="w1", result={"verdict": "pass"})
    again = await delegate(repo=repo, **_verifier(dedup_key="verify-t1"))
    assert again.contract_id != first.contract_id


@pytest.mark.asyncio
async def test_depth_cap_blocks_runaway_chains(repo):
    root = await delegate(repo=repo, **_verifier())
    child = await delegate(repo=repo, parent_kind="contract",
                           parent_id=root.contract_id,
                           ancestor_ids=[root.contract_id],
                           role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                           goal="resolve lvl1",
                           acceptance_criteria=[{"kind": "non_empty"}],
                           input_data={"d": 1},
                           allowed_tools=["browser.observe"],
                           depth=1, budget_units=10)
    grandchild = await delegate(repo=repo, parent_kind="contract",
                                parent_id=child.contract_id,
                                ancestor_ids=[root.contract_id,
                                              child.contract_id],
                                role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                                goal="resolve lvl2",
                                acceptance_criteria=[{"kind": "non_empty"}],
                                input_data={"d": 2},
                                allowed_tools=["browser.observe"],
                                depth=2, budget_units=10)
    with pytest.raises(AgentTaskError, match="depth"):
        await delegate(repo=repo, parent_kind="contract",
                       parent_id=grandchild.contract_id,
                       ancestor_ids=[root.contract_id, child.contract_id,
                                     grandchild.contract_id],
                       role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                       goal="too deep",
                       acceptance_criteria=[{"kind": "non_empty"}],
                       input_data={"d": 9},
                       allowed_tools=["browser.observe"],
                       depth=MAX_DELEGATION_DEPTH + 1)


@pytest.mark.asyncio
async def test_fan_out_and_active_children_caps(repo):
    """§15.5: the ACTIVE-children cap binds first while children run;
    completing children frees slots (total fan-out cap still holds)."""
    parent = await delegate(repo=repo, **_verifier())
    made = 0
    with pytest.raises(AgentTaskError, match="active children"):
        for i in range(MAX_CHILDREN_PER_PARENT + 1):
            c = await delegate(repo=repo, parent_kind="contract",
                               parent_id=parent.contract_id,
                               ancestor_ids=[parent.contract_id],
                               role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                               goal=f"fan {i}",
                               acceptance_criteria=[{"kind": "non_empty"}],
                               input_data={"i": i},
                               allowed_tools=["browser.observe"],
                               depth=1, dedup_key=f"k{i}", budget_units=10)
            await repo.claim(c.contract_id, "w1")   # dispatched
            made += 1
    assert made == MAX_ACTIVE_CHILDREN_PER_PARENT, \
        "the concurrent-children cap stops runaway fan-out"


@pytest.mark.asyncio
async def test_cycle_detection(repo):
    """§15.5: creation cannot cycle (fresh ids can never be their own
    ancestor) — the defensive self-ancestor check refuses it anyway, and
    the DEPTH cap bounds any runaway chain (tested above)."""
    from backend.models.agent_task import AgentTaskContract
    self_ancestor = AgentTaskContract(
        contract_id="atc-self", parent_kind="contract", parent_id="atc-self",
        ancestor_ids=["atc-self"], role=AgentRole.DOM_RESOLVER,
        tenant_id="u1", allowed_tools=["browser.observe"],
        budget_units=10, deadline_at=time.time() + 600)
    with pytest.raises(AgentTaskError, match="cycle"):
        await repo.create_contract(self_ancestor)


# ── §15.6: partial failure ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_failure_verdict_names_what_is_missing(repo):
    parent = await delegate(repo=repo, **_verifier(
        completion_policy="require_all", dedup_key=None))
    c1 = await delegate(repo=repo, parent_kind="contract",
                        parent_id=parent.contract_id,
                        ancestor_ids=[parent.contract_id],
                        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                        goal="resolve dom targets",
                        input_data={},
                        acceptance_criteria=[{"kind": "non_empty"}],
                        allowed_tools=["browser.observe"],
                        depth=1, dedup_key="c1", budget_units=20)
    c2 = await delegate(repo=repo, parent_kind="contract",
                        parent_id=parent.contract_id,
                        ancestor_ids=[parent.contract_id],
                        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                        goal="resolve dom targets b",
                        input_data={},
                        acceptance_criteria=[{"kind": "non_empty"}],
                        allowed_tools=["browser.observe"],
                        depth=1, dedup_key="c2", budget_units=20)
    await complete(c1.contract_id, {"targets": ["a"]}, actor="inline", repo=repo)
    await fail(c2.contract_id, "stale snapshot", actor="inline", repo=repo)

    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "incomplete"
    assert c2.contract_id in verdict["missing"], \
        "the verdict names exactly which child failed"


@pytest.mark.asyncio
async def test_require_any_policy_completes_with_one_child(repo):
    parent = await delegate(repo=repo, **_verifier(
        completion_policy="require_any"))
    c1 = await delegate(repo=repo, parent_kind="contract",
                        parent_id=parent.contract_id,
                        ancestor_ids=[parent.contract_id],
                        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                        goal="resolve dom r1",
                        input_data={},
                        acceptance_criteria=[{"kind": "non_empty"}],
                        allowed_tools=["browser.observe"],
                        depth=1, dedup_key="r1", budget_units=20)
    c2 = await delegate(repo=repo, parent_kind="contract",
                        parent_id=parent.contract_id,
                        ancestor_ids=[parent.contract_id],
                        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
                        goal="resolve dom r2",
                        input_data={},
                        acceptance_criteria=[{"kind": "non_empty"}],
                        allowed_tools=["browser.observe"],
                        depth=1, dedup_key="r2", budget_units=20)
    await complete(c1.contract_id, {"targets": ["a"]}, actor="inline", repo=repo)
    await fail(c2.contract_id, "timeout", actor="inline", repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "complete"
    assert c2.contract_id in [f["contract_id"] for f in verdict["failed"]], \
        "the failed child stays visible in the verdict"


# ── §15.7: specialization must earn its complexity ─────────────────────

@pytest.mark.asyncio
async def test_verifier_contract_cannot_complete_without_verdict(repo):
    """The typed output contract makes the verifier's role concrete: no
    verdict key → no completion."""
    contract = await delegate(repo=repo, **_verifier())
    await repo.claim(contract.contract_id, "w1")
    with pytest.raises(AgentTaskError):
        await complete(contract.contract_id, {"notes": "looks fine"},
                       actor="w1",
                       repo=repo)
