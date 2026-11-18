"""Verify acceptance, wake recovery and duplicate resolution on the emulator."""
import pytest

from backend.tests.test_phase26_feedback_firestore import fs_client, stack  # noqa: F401
from backend.services.connector_build_service import request_durable_build, ORIGIN_ORCHESTRATOR


@pytest.mark.asyncio
async def test_factory_build_wait_survives_restart_and_publishes_exactly_one_wake(stack, fs_client):
    _, _, _, factory = stack
    tracking = await request_durable_build(factory, user_id="owner", method="spec", source="{}",
        name="wait-storage-fixture", origin=ORIGIN_ORCHESTRATOR, parent_run_id="run", parent_task_id="task",
        required_operations=["read"])
    wait = await factory.wait_repo.find_open_wait("run", "task")
    assert wait["child_run_id"] == tracking["run_id"]
    assert await factory.wait_repo.create_wait("run", "task", child_run_id=tracking["run_id"]) == wait["wait_id"]
    from backend.repositories.firestore.wait_repository import FirestoreWaitRepository
    recovered = FirestoreWaitRepository()
    assert [w["wait_id"] for w in await recovered.unresolved_waits_for_child(tracking["run_id"])] == [wait["wait_id"]]
    # Test only persistence here. Real worker eligibility and dynamic tool
    # output are independently exercised by the PostgreSQL journey.
    wake = {"run_id": "run", "task_id": "task", "child_run_id": tracking["run_id"]}
    assert await recovered.resolve_with_wake(wait, wake)
    assert not await factory.wait_repo.resolve_with_wake(wait, wake)
    assert await recovered.find_open_wait("run", "task") is None
    assert (await recovered.latest_wait("run", "task"))["state"] == "resolved"
    events = await factory.command_repo.claim_outbox_batch("test", 10, 30)
    wakes = [event for event in events if event.event_type == "parent.ready"]
    assert len(wakes) == 1 and wakes[0].payload == wake
    assert await recovered.create_wait("run", "task", child_run_id="next-child") != wait["wait_id"]
