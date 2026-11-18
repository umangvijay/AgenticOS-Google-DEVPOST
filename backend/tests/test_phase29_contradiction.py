"""Phase 29 — contradictory sibling results at the join (§29.22).

Siblings asked the SAME question (same role + normalized goal) that
return DIFFERENT conclusions surface a 'disputed' verdict with both
positions named — the join never silently picks a side. Agreement and
different-question siblings are unaffected.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from backend.models.agent_task import AgentRole
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, set_agent_task_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_contracts import complete, delegate


_CRITERIA = [{"kind": "non_empty", "description": "typed output exists"}]


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "p29-contradiction.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


async def _child(repo, parent_id, role, goal, result=None):
    contract = await delegate(
        parent_kind="run", parent_id=parent_id, role=role, tenant_id="u1",
        goal=goal, input_data={"x": 1}, budget_units=40,
        output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], repo=repo)
    await repo.claim(contract.contract_id, "worker-x")
    if result is not None:
        await complete(contract.contract_id, result, actor="worker-x",
                       repo=repo)
    return contract


def _parent(repo, run_id):
    return delegate(
        parent_kind="run", parent_id=run_id, role=AgentRole.DELIVERABLE_ASSEMBLER,
        tenant_id="u1", goal="assemble deliverable", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], repo=repo)


@pytest.mark.asyncio
async def test_contradictory_sibling_conclusions_surface_disputed(repo):
    """Two verifier siblings asked the same question disagree — the join
    is 'disputed' with both positions named, never a silent success."""
    parent = await _parent(repo, "run-disputed")
    a = await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                     "verify the deliverable",
                     result={"ok": True, "verdict": "pass"})
    b = await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                     "verify the deliverable",
                     result={"ok": True, "verdict": "fail"})
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "disputed", verdict
    assert len(verdict["contradictions"]) == 1
    positions = verdict["contradictions"][0]["positions"]
    values = {p["conclusion"] for p in positions}
    assert values == {"pass", "fail"}
    named = {cid for p in positions for cid in p["contract_ids"]}
    assert {a.contract_id, b.contract_id} <= named


@pytest.mark.asyncio
async def test_agreeing_siblings_do_not_dispute(repo):
    parent = await _parent(repo, "run-agreed")
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable",
                 result={"ok": True, "verdict": "pass"})
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable",
                 result={"ok": True, "verdict": "pass"})
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "complete"
    assert verdict["contradictions"] == []


@pytest.mark.asyncio
async def test_same_role_different_questions_are_not_compared(repo):
    """Two children with the same role but DIFFERENT questions returning
    different conclusions is not a contradiction — they answer different
    things."""
    parent = await _parent(repo, "run-different")
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable layout",
                 result={"ok": True, "verdict": "pass"})
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable facts",
                 result={"ok": True, "verdict": "fail"})
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "complete"
    assert verdict["contradictions"] == []


@pytest.mark.asyncio
async def test_pending_children_still_wait_despite_contradiction(repo):
    """A join with pending children waits — contradiction checks do not
    promote a waiting join to a decision."""
    parent = await _parent(repo, "run-waiting")
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable",
                 result={"ok": True, "verdict": "pass"})
    await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                 "verify the deliverable")  # never completed
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "waiting"
