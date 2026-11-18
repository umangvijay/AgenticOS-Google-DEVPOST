"""Phase 26 R4. Persisted authority, typed results and spend across storage profiles."""
import pytest

from backend.models.agent_task import AgentRole, AgentTaskError, AgentTaskState
from backend.services.agent_contracts import delegate
from backend.services.agent_task_contracts import delegate_specialist
from backend.tests.test_phase15_agents import repo  # noqa: F401
from backend.tests.test_phase26_feedback_firestore import fs_client  # noqa: F401


async def check_completion(repo):
    work = await delegate(
        parent_kind="run", parent_id="parity-run", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="owner", goal="Check the original result", input_data={"expected": 31},
        exclusions=["no writes"], input_refs=["artifact-1"], required_capabilities=["criteria.verify"],
        output_schema={"required": ["verified"]}, allowed_tools=["evidence.read"],
        acceptance_criteria=[{"kind": "matches_reference"}], budget_units=3,
        retry_policy="none", agent_version="av", tool_version="tv", policy_version="pv", repo=repo)
    claimed = await repo.claim(work.contract_id, "verifier")
    for result, spent, code in [
        (None, 0, "OUTPUT_SCHEMA_VIOLATION"), ({"verified": None}, 0, "OUTPUT_SCHEMA_VIOLATION"),
        ({"verified": False}, -1, "BUDGET_EXCEEDED"), ({"verified": False}, 4, "BUDGET_EXCEEDED"),
    ]:
        with pytest.raises(AgentTaskError) as error:
            await repo.transition(work.contract_id, AgentTaskState.COMPLETED, actor="verifier",
                                  result=result, spent_units=spent)
        assert error.value.code == code
        assert await repo.get_contract(work.contract_id) == claimed
    done = await repo.transition(work.contract_id, AgentTaskState.COMPLETED, actor="verifier",
                                 result={"verified": False}, spent_units=3)
    assert done.result == {"verified": False} and done.spent_units == 3
    assert done.goal == "Check the original result" and done.exclusions == ["no writes"]
    assert done.input_refs == ["artifact-1"] and done.required_capabilities == ["criteria.verify"]
    assert done.acceptance_criteria == [{"kind": "matches_reference"}]
    assert (done.retry_policy, done.agent_version, done.tool_version, done.policy_version) == ("none", "av", "tv", "pv")


@pytest.mark.asyncio
async def test_sqlite_completion_requires_output_and_budget(repo):
    await check_completion(repo)


@pytest.mark.asyncio
async def test_firestore_completion_requires_output_and_budget(fs_client, monkeypatch):
    from backend.repositories.firestore.database import FirestoreDB
    from backend.repositories.firestore.agent_task_repository import FirestoreAgentTaskRepository
    monkeypatch.setattr(FirestoreDB, "_instance", fs_client)
    await check_completion(FirestoreAgentTaskRepository())


@pytest.mark.asyncio
async def test_parent_can_only_narrow_specialist_grants(repo):
    task = await delegate_specialist(AgentRole.CONNECTOR_VALIDATOR, parent_kind="run",
        parent_id="scope-run", tenant_id="owner",
        input_data={"operation_id": "read", "memory_scope": {
            "classes": ["factual", "private"], "topics": ["validation", "conversation"], "all": True}},
        output_schema={"required": ["extra_check"]}, repo=repo)
    scope = task.input_data["memory_scope"]
    assert scope["classes"] == ["factual"] and scope["topics"] == ["validation"] and "all" not in scope
    assert set(task.output_schema["required"]) == {"operation_id", "test_cases", "extra_check"}


@pytest.mark.asyncio
async def test_specialist_memory_is_filtered_before_ranking_and_limiting(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.memory_repository import SQLiteMemoryRepository
    from backend.services.memory_service import ApplicationMemoryService

    db = DatabaseManager(str(tmp_path / "memory.db"))
    await db.initialize()
    try:
        service = ApplicationMemoryService(SQLiteMemoryRepository(db))
        for owner, content, topic in [
            ("owner", "ledger evidence", "validation"),
            ("owner", "ledger private target", "conversation"),
            ("owner", "ledger old untagged", None),
            ("peer", "ledger third party", "validation"),
        ]:
            await service.remember(user_id=owner, content=content, explicit=True,
                                   metadata={"topic": topic} if topic else {})
        for embedding in ([], [1.0, 0.0]):
            result = await service.retrieve_for_task(user_id="owner", query="ledger", limit=1,
                query_embedding=embedding, specialist_role="connector_validator",
                memory_scope={"topics": ["validation", "conversation"]})
            assert [row["content"] for row in result["results"]] == ["ledger evidence"]
        denied = await service.retrieve_for_task(user_id="owner", query="ledger",
            specialist_role="connector_validator", memory_scope={"topics": []})
        assert denied["results"] == []
        ordinary = await service.retrieve_for_task(user_id="owner", query="ledger")
        assert len(ordinary["results"]) == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_firestore_worker_loss_deadline_and_dedup(fs_client, monkeypatch):
    from backend.repositories.firestore import agent_task_repository as module
    from backend.repositories.firestore.database import FirestoreDB
    monkeypatch.setattr(FirestoreDB, "_instance", fs_client)
    worker = module.FirestoreAgentTaskRepository()
    replacement = module.FirestoreAgentTaskRepository()
    now = module.epoch_now()
    monkeypatch.setattr(module, "epoch_now", lambda: now)
    async def create():
        return await delegate_specialist(AgentRole.RELEASE_VERIFIER, parent_kind="run",
            parent_id="lease-run", tenant_id="owner", input_data={"candidate": "a"},
            deadline_seconds=30, dedup_key="verify", repo=worker)

    task = await create()
    claimed = await worker.claim(task.contract_id, "original", lease_seconds=4)
    assert claimed.state is AgentTaskState.RUNNING
    assert await replacement.claim(task.contract_id, "replacement") is None
    assert await worker.renew_lease(task.contract_id, "original", lease_seconds=4)
    now += 5
    assert task.contract_id in {c.contract_id for c in await replacement.reclaimable_contracts()}
    assert (await replacement.claim(task.contract_id, "replacement")).owner_worker_id == "replacement"
    with pytest.raises(AgentTaskError) as refused:
        await worker.transition(task.contract_id, AgentTaskState.COMPLETED, actor="original",
                                result={"checks": [], "summary": "stale"})
    assert refused.value.code == "CLAIM_FENCED"
    now = task.deadline_at + 1
    assert not await replacement.renew_lease(task.contract_id, "replacement")
    assert await replacement.expire_stale_contracts(now=now) == 1
    assert await worker.expire_stale_contracts(now=now) == 0
    assert (await worker.get_contract(task.contract_id)).state is AgentTaskState.TIMED_OUT
    retry = await create()
    assert retry.contract_id != task.contract_id
    assert await worker.claim(retry.contract_id, "worker") is None
    assert (await worker.get_contract(retry.contract_id)).state is AgentTaskState.TIMED_OUT
    assert (await create()).contract_id != retry.contract_id
