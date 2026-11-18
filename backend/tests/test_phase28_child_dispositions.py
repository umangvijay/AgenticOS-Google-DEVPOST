"""Phase 28 §28.5 — the parent-disposition drills that lacked named
coverage.

The enabled-role journey matrix (test_phase27_agent_roles_journey), the
typed-specialist suites (claim races, budget caps, tree cancellation,
queue recovery, revocation) and the coordination suites (fan-out,
duplicate dispatch, crash recovery) are retained. This file pins the
remaining close-when rule: ONE failed required child alongside a
successful sibling must block verified success, and an OPTIONAL child's
failure settles under the declared partial-success contract — named,
never silent.
"""
import pytest
import pytest_asyncio

from backend.models.agent_task import AgentRole
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, set_agent_task_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_contracts import complete, delegate, fail


_CRITERIA = [{"kind": "non_empty", "description": "typed output exists"}]


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "p28-dispositions.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


async def _child(repo, parent_id, role, goal, **kw):
    return await delegate(
        parent_kind="run", parent_id=parent_id, role=role, tenant_id="u1",
        goal=goal, input_data={"x": 1}, budget_units=40,
        output_schema={"required": ["ok"]},
        acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], repo=repo, **kw)


@pytest.mark.asyncio
async def test_failed_required_child_blocks_success_alongside_successful_sibling(repo):
    """require_all: one FAILED child + one COMPLETED sibling → the parent
    verdict is 'incomplete' naming the failed child — never success."""
    parent = await delegate(
        parent_kind="run", parent_id="run-required", role=AgentRole.DELIVERABLE_ASSEMBLER,
        tenant_id="u1", goal="assemble deliverable", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], completion_policy="require_all",
        repo=repo)
    good = await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                        "verify the assembled deliverable")
    bad = await _child(repo, parent.contract_id, AgentRole.BUILD_VERIFIER,
                       "verify the build that feeds the deliverable")
    await repo.claim(good.contract_id, "worker-good")
    await complete(good.contract_id, {"ok": True}, actor="worker-good", repo=repo)
    await repo.claim(bad.contract_id, "worker-bad")
    await fail(bad.contract_id, "source artifact missing", actor="worker-bad",
               repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "incomplete"
    assert verdict["policy"] == "require_all"
    assert verdict["failed"] == [
        {"contract_id": bad.contract_id, "state": "FAILED",
         "error": "source artifact missing"}]
    assert good.contract_id in verdict["completed"]
    assert bad.contract_id in verdict["missing"]


@pytest.mark.asyncio
async def test_optional_child_failure_settles_named_under_best_effort(repo):
    """best_effort (the explicitly defined partial-success contract): the
    verdict is 'complete' — but the failed child is NAMED in the verdict,
    never silently dropped."""
    parent = await delegate(
        parent_kind="run", parent_id="run-optional", role=AgentRole.DELIVERABLE_ASSEMBLER,
        tenant_id="u1", goal="assemble deliverable", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], completion_policy="best_effort",
        repo=repo)
    core = await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER,
                        "verify the deliverable")
    optional = await _child(repo, parent.contract_id,
                            AgentRole.DOCUMENT_QUALITY_REVIEWER,
                            "advisory layout review")
    await repo.claim(core.contract_id, "worker-core")
    await complete(core.contract_id, {"ok": True}, actor="worker-core", repo=repo)
    await repo.claim(optional.contract_id, "worker-opt")
    await fail(optional.contract_id, "reviewer unavailable", actor="worker-opt",
               repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "complete"
    assert verdict["policy"] == "best_effort"
    # The partial result is explicit: who failed and why.
    assert verdict["failed"] == [
        {"contract_id": optional.contract_id, "state": "FAILED",
         "error": "reviewer unavailable"}]
    assert verdict["missing"] == [optional.contract_id]
    assert core.contract_id in verdict["completed"]


@pytest.mark.asyncio
async def test_parent_waits_while_a_sibling_is_still_pending(repo):
    """A pending sibling holds the verdict at 'waiting' — neither the
    failed child nor the completed one settles the parent early."""
    parent = await delegate(
        parent_kind="run", parent_id="run-waiting", role=AgentRole.DELIVERABLE_ASSEMBLER,
        tenant_id="u1", goal="assemble deliverable", input_data={"x": 1},
        output_schema={"required": ["ok"]}, acceptance_criteria=_CRITERIA,
        allowed_tools=["evidence.read"], completion_policy="require_all",
        repo=repo)
    done = await _child(repo, parent.contract_id, AgentRole.OUTCOME_VERIFIER, "v")
    pending = await _child(repo, parent.contract_id, AgentRole.BUILD_VERIFIER, "b")
    await repo.claim(done.contract_id, "worker-a")
    await complete(done.contract_id, {"ok": True}, actor="worker-a", repo=repo)
    verdict = await repo.evaluate_completion_policy(parent.contract_id)
    assert verdict["verdict"] == "waiting"
    assert pending.contract_id in verdict["pending"]
