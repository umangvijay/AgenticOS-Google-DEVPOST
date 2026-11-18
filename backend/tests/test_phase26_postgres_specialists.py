"""Phase 26 R4. Use the real factory and a second pool in the same test-owned schema."""
import asyncio

import pytest
import pytest_asyncio

from backend.agents.connector_validator.validator_agent import ValidationContext, run_validator_via_contract
from backend.agents.verifier.verifier_agent import VerificationContext, run_verifier_via_contract
from backend.models.agent_task import AgentRole, AgentTaskError, AgentTaskState
from backend.repositories.agent_task_repository import get_agent_task_repo
from backend.repositories.postgres.agent_task_repository import PostgresAgentTaskRepository
from backend.services.agent_authority import AuthorizationLost
from backend.services.agent_contracts import cancel, complete, delegate
from backend.services.agent_task_contracts import delegate_specialist
from backend.tests.test_phase26_connector_journey import stack  # noqa: F401


@pytest_asyncio.fixture
async def pools(stack):
    # The journey fixture routes factory construction to its own schema. Use the
    # real manager type from the initialized factory for the independent worker.
    factory, _, owner, *_ = stack
    db = type(factory.db_manager)(factory.db_manager._dsn, schema=factory.db_manager._schema)
    await db.initialize()
    try:
        assert get_agent_task_repo() is factory.agent_task_repo
        yield factory, PostgresAgentTaskRepository(db), owner
    finally:
        await db.close()


def verifier_context():
    return VerificationContext(task_id="criterion-check", run_id="local-run", goal="Verify the output",
        criteria=[{"id": "present", "description": "output required"}], output_data={},
        deterministic_verdict="fail", deterministic_reasons=["output missing"])


@pytest.mark.asyncio
async def test_typed_specialists_authorize_owner_and_persist_outputs(pools):
    factory, reader, owner = pools
    repo = factory.agent_task_repo
    verified = await run_verifier_via_contract(repo, verifier_context(), principal_id=owner, parent_id="run-v")
    assert verified["combined_verdict"] == "fail"
    first = await reader.get_contract(verified["contract_id"])
    assert first.state is AgentTaskState.COMPLETED and first.tenant_id == owner
    assert first.result["deterministic_verdict"] == "fail"

    ctx = ValidationContext(connector_id="fixture-connector", operation_id="readEntries",
                            operation_schema={"paginated": True})
    proposed = await run_validator_via_contract(reader, ctx, principal_id=owner, parent_id="run-c")
    plan = (await repo.get_contract(proposed["contract_id"])).result
    assert plan["operation_id"] == ctx.operation_id
    assert {"pagination", "auth_denial"} <= {case["kind"] for case in plan["test_cases"]}

    await factory.user_repo.set_user_active(owner, False)
    with pytest.raises(AuthorizationLost):
        await run_verifier_via_contract(reader, verifier_context(), principal_id=owner, parent_id="run-revoked")
    assert not await repo.children_of("run-revoked")


async def assignment(repo, owner, parent, goal, *, nested=False, budget=10):
    return await delegate(parent_kind="contract" if nested else "run", parent_id=parent,
        tenant_id=owner, role=AgentRole.RELEASE_VERIFIER, input_data={"goal": goal}, goal=goal,
        output_schema={"required": ["summary"]}, allowed_tools=["evidence.read"],
        budget_units=budget, acceptance_criteria=[{"kind": "non_empty"}], repo=repo)


@pytest.mark.asyncio
async def test_claim_races_budget_and_atomic_tree_cancellation(pools):
    factory, worker, owner = pools
    repo = factory.agent_task_repo
    duplicates = await asyncio.gather(assignment(repo, owner, "run", "audit"),
        assignment(worker, owner, "run", "audit"), return_exceptions=True)
    assert sum(isinstance(x, AgentTaskError) for x in duplicates) == 1
    root, = await repo.children_of("run")
    child = await assignment(worker, owner, root.contract_id, "review", nested=True, budget=6)
    with pytest.raises(AgentTaskError, match="budget"):
        await assignment(repo, owner, root.contract_id, "overspend", nested=True, budget=5)
    with pytest.raises(AgentTaskError, match="different tenant"):
        await assignment(repo, "other-user", root.contract_id, "foreign", nested=True, budget=1)
    grand = await assignment(repo, owner, child.contract_id, "inspect", nested=True, budget=2)
    wins = await asyncio.gather(repo.claim(grand.contract_id, "one"), worker.claim(grand.contract_id, "two"))
    winner, = [c.owner_worker_id for c in wins if c is not None]
    assert await worker.renew_lease(grand.contract_id, winner)
    with pytest.raises(AgentTaskError, match="live claim"):
        await complete(grand.contract_id, {"summary": "late"}, actor="other", repo=worker)
    with pytest.raises(AgentTaskError, match="budget"):
        await complete(grand.contract_id, {"summary": "over"}, actor=winner, spent_units=3, repo=worker)
    with pytest.raises(AgentTaskError, match="required"):
        await complete(grand.contract_id, {"summary": None}, actor=winner, repo=worker)
    assert (await worker.get_contract(grand.contract_id)).spent_units == 0
    cancelled = await cancel(root.contract_id, repo=worker)
    assert cancelled["children_cancelled"] == 2
    assert all(c.state is AgentTaskState.CANCELLED for c in await repo.subtree_of("run"))
    assert (await cancel(root.contract_id, repo=repo))["children_cancelled"] == 0
    with pytest.raises(AgentTaskError):
        await assignment(worker, owner, grand.contract_id, "late", nested=True, budget=1)
    assert await worker.pending_contracts() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["validator", "verifier"])
async def test_specialist_without_claim_never_invokes_judge(pools, monkeypatch, role):
    factory, worker, owner = pools
    repo = factory.agent_task_repo
    async def claimed_elsewhere(contract_id, _actor):
        assert await worker.claim(contract_id, "different-worker")
        return None

    monkeypatch.setattr(repo, "claim", claimed_elsewhere)
    async def forbidden(*args):
        pytest.fail("Unclaimed work reached the judge")

    with pytest.raises(AgentTaskError, match="could not be claimed"):
        if role == "verifier":
            await run_verifier_via_contract(repo, verifier_context(), principal_id=owner,
                                            parent_id="run", judge_client=forbidden)
        else:
            await run_validator_via_contract(repo, ValidationContext(connector_id="c", operation_id="read"),
                                             principal_id=owner, parent_id="run", judge_client=forbidden)
    contract, = await worker.children_of("run")
    assert contract.state is AgentTaskState.RUNNING and contract.owner_worker_id == "different-worker"
    with pytest.raises(AgentTaskError, match="no allowed_tools"):
        await delegate_specialist(AgentRole.RELEASE_VERIFIER, parent_kind="run", parent_id="no-grant",
                                  tenant_id=owner, input_data={"candidate": "v1"}, allowed_tools=[], repo=repo)


@pytest.mark.asyncio
async def test_workspace_shows_and_cancels_only_authorized_delegations(pools):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    from backend.api.routers.workflows import router

    factory, reader, owner = pools
    run_id = "delegation-panel-run"
    await factory.workflow_repo.save_run({"run_id": run_id, "user_id": owner, "goal": "verify output",
                                          "status": "RUNNING", "tasks": []})
    contract = await assignment(reader, owner, run_id, "review evidence")
    app = FastAPI()
    app.state.factory = factory
    app.include_router(router)
    actor = {"id": "peer", "role": "user"}

    async def identity():
        return AuthenticatedUser(actor["id"], "fixture@example.test", "Fixture", actor["role"], "local")

    app.dependency_overrides[get_current_user] = identity
    path = f"/workflows/{run_id}/delegations"
    action = f"{path}/{contract.contract_id}/cancel"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.get(path)).status_code == 403
        assert (await client.post(action)).status_code == 403
        actor.update(id=owner, role="viewer")
        assert (await client.post(action)).status_code == 403
        assert (await reader.get_contract(contract.contract_id)).state is AgentTaskState.PROPOSED
        actor["role"] = "user"
        rows = (await client.get(path)).json()["delegations"]
        assert [row["contract_id"] for row in rows] == [contract.contract_id]
        assert (await client.post(action)).json()["state"] == "CANCELLED"
        assert (await reader.get_contract(contract.contract_id)).state is AgentTaskState.CANCELLED


@pytest.mark.asyncio
async def test_queue_recovery_and_four_simultaneous_claims(pools, monkeypatch):
    import backend.repositories.postgres.agent_task_repository as pg
    factory, replica, owner = pools
    api = factory.agent_task_repo
    tasks = [await assignment(api, owner, "recover", f"work-{i}") for i in range(5)]
    assert {c.contract_id for c in await replica.pending_contracts()} == {c.contract_id for c in tasks}
    starts = await asyncio.gather(*(replica.claim(c.contract_id, f"worker-{i}", lease_seconds=5)
                                    for i, c in enumerate(tasks)))
    assert sum(c is not None for c in starts) == 4
    running = next(c for c in starts if c)
    monkeypatch.setattr(pg, "epoch_now", lambda: running.lease_expires_at + 1)
    assert running.contract_id in {c.contract_id for c in await api.reclaimable_contracts()}
    assert (await replica.claim(running.contract_id, "replacement")).state is AgentTaskState.RUNNING
    with pytest.raises(AgentTaskError, match="live claim"):
        await complete(running.contract_id, {"summary": "stale"}, actor=running.owner_worker_id, repo=api)
    done = await complete(running.contract_id, {"summary": "verified"}, actor="replacement", spent_units=2, repo=api)
    assert done.result == {"summary": "verified"} and done.spent_units == 2
    assert await replica.expire_stale_contracts(now=tasks[-1].deadline_at + 1) == 4
    assert (await replica.evaluate_completion_policy("recover"))["verdict"] == "incomplete"


@pytest.mark.asyncio
async def test_account_revoked_during_execution_blocks_publication(pools):
    factory, reader, owner = pools
    async def review(_context):
        await factory.user_repo.set_user_active(owner, False)
        return {"verdict": "pass", "criterion_results": [], "reasoning": "judge finished"}

    with pytest.raises(AuthorizationLost):
        await run_verifier_via_contract(factory.agent_task_repo, verifier_context(), principal_id=owner,
                                        parent_id="run-recheck", judge_client=review)
    failed, = await reader.children_of("run-recheck")
    assert failed.state is AgentTaskState.FAILED and failed.result is None
