"""Phase 10 §10.21-10.23: execution-step repository, state machine, and the
§10.1 workflow dedup fix — through service/repo boundaries."""
import pytest
import pytest_asyncio

from backend.models.effect import EffectState


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "steps.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    from backend.repositories.execution_step_repository import ExecutionStepRepository
    return ExecutionStepRepository(db)


# ── §10.6: the step state machine ─────────────────────────────────────

@pytest.mark.asyncio
async def test_step_lifecycle_happy_path(repo):
    from backend.repositories.execution_step_repository import StepState
    step = await repo.create_step({
        "step_id": "step-1", "logical_action_id": "la-1",
        "run_id": "r1", "task_id": "t1", "tenant_id": "u1",
        "sequence": 1, "proposed_action": "click",
        "validated_action_digest": "digest-1",
    })
    assert step["state"] == "OBSERVING"
    step = await repo.transition("step-1", StepState.PROPOSED, actor="agent")
    step = await repo.transition("step-1", StepState.VALIDATING, actor="system")
    step = await repo.transition("step-1", StepState.READY, actor="system")
    step = await repo.transition("step-1", StepState.DISPATCHING, actor="agent",
                                 evidence_reference="effect-1")
    step = await repo.transition("step-1", StepState.VERIFYING, actor="system")
    step = await repo.transition("step-1", StepState.VERIFIED, actor="verifier",
                                 result_reference="result-ref-1")
    assert step["state"] == "VERIFIED"
    assert "effect-1" in step["evidence_references"]
    assert step["result_reference"] == "result-ref-1"


@pytest.mark.asyncio
async def test_dispatching_to_outcome_uncertain(repo):
    """§10.6: DISPATCHING → OUTCOME_UNCERTAIN is appropriate when an
    external action may have happened but the result was lost — a retry
    must not convert that directly into another dispatch."""
    from backend.repositories.execution_step_repository import StepState
    await repo.create_step({
        "step_id": "step-u", "logical_action_id": "la-u",
        "run_id": "r1", "task_id": "t1", "tenant_id": "u1", "sequence": 1})
    for state in (StepState.PROPOSED, StepState.VALIDATING, StepState.READY,
                  StepState.DISPATCHING):
        await repo.transition("step-u", state, actor="system")
    step = await repo.transition("step-u", StepState.OUTCOME_UNCERTAIN,
                                 actor="system", error_code="RESPONSE_LOST")
    assert step["state"] == "OUTCOME_UNCERTAIN"
    assert step["error_code"] == "RESPONSE_LOST"
    # OUTCOME_UNCERTAIN is quiescent: no direct dispatch from it.
    from backend.repositories.execution_step_repository import step_transition_valid
    assert not step_transition_valid(StepState.OUTCOME_UNCERTAIN,
                                     StepState.DISPATCHING, "agent")


@pytest.mark.asyncio
async def test_cancellation_races_completion_cas(repo):
    """§10.6/§10.15 acceptance: concurrent cancellation and completion
    cannot produce contradictory terminal states — the CAS guarantees one
    winner."""
    from backend.repositories.execution_step_repository import (
        StepState, StepTransitionError)
    await repo.create_step({
        "step_id": "step-race", "logical_action_id": "la-race",
        "run_id": "r1", "task_id": "t1", "tenant_id": "u1", "sequence": 1})
    await repo.transition("step-race", StepState.PROPOSED, actor="agent")
    await repo.transition("step-race", StepState.VALIDATING, actor="system")
    await repo.transition("step-race", StepState.READY, actor="system")
    await repo.transition("step-race", StepState.DISPATCHING, actor="agent")
    await repo.transition("step-race", StepState.VERIFYING, actor="system")
    # Cancellation wins the CAS (expected state = VERIFYING).
    cancelled = await repo.transition(
        "step-race", StepState.CANCELLED, actor="operator",
        expected_state=StepState.VERIFYING)
    assert cancelled["state"] == "CANCELLED"
    # Completion now conflicts (the state has changed).
    with pytest.raises(Exception):
        await repo.transition(
            "step-race", StepState.VERIFIED, actor="verifier",
            expected_state=StepState.VERIFYING)


# ── §10.1: workflow dedup returns the original run ────────────────────

@pytest.mark.asyncio
async def test_workflow_dedup_returns_original_run(repo):
    """§10.1/§10.23 item 1: workflow acceptance deduplication returns the
    original run — a repeated request does not replan a new run."""
    from backend.services.command_service import accept_command
    from backend.services.command_service import set_command_repo
    from backend.repositories.command_repository import SQLiteCommandRepository
    set_command_repo(repo) if hasattr(repo, 'db') else None
    # Use the command repo from the same db
    from backend.repositories.command_repository import SQLiteCommandRepository as _CR
    set_command_repo(_CR(repo.db))
    try:
        import hashlib, json as _json
        digest = hashlib.sha256(_json.dumps(
            {"goal": "do the thing", "model": None, "thread_id": None,
             "parent_run_id": None}, sort_keys=True).encode()).hexdigest()
        r1 = await accept_command(
            command_type="workflow", owner_user_id="u1", source="api",
            idempotency_key="key-1", request_digest=digest,
            payload={"goal": "do the thing"}, run_id="run-original",
            max_pending_per_user=5,
        )
        assert r1.accepted and not r1.deduplicated
        assert r1.run_id == "run-original"
        # Same key + same digest → the original command/run is returned.
        r2 = await accept_command(
            command_type="workflow", owner_user_id="u1", source="api",
            idempotency_key="key-1", request_digest=digest,
            payload={"goal": "do the thing"}, run_id="run-SECOND",
            max_pending_per_user=5,
        )
        assert r2.deduplicated is True
        assert r2.run_id == "run-original"   # NOT run-SECOND
        # Same key + different digest → conflict.
        r3 = await accept_command(
            command_type="workflow", owner_user_id="u1", source="api",
            idempotency_key="key-1", request_digest="digest-DIFFERENT",
            payload={"goal": "different thing"}, run_id="run-THIRD",
            max_pending_per_user=5,
        )
        assert r3.accepted is False
        assert r3.error_code.value == "IDEMPOTENCY_CONFLICT"
    finally:
        set_command_repo(None)
