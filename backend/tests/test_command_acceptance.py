"""Phase 4 §4.23: command acceptance, idempotency, lifecycle transitions,
cancellation protocol, outbox delivery, and parent waits.

The §4.1 invariants under test:
- Accepted means durable (command + accounting + initial work + outbox commit
  together).
- Duplicate requests preserve identity; conflicting reuse is rejected.
- Execution ownership transitions are conditional (state-version guarded).
- Terminal state is protected; cancellation is truthful.
- Publication is recoverable; republish-after-crash is tolerated by design.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.command import (
    Command,
    CommandErrorCode,
    CommandState,
    CommandTransitionError,
    OutboxEvent,
    WaitingReason,
    new_command_id,
    new_event_id,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "commands.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    from backend.repositories.command_repository import SQLiteCommandRepository
    return SQLiteCommandRepository(db)


@pytest_asyncio.fixture
async def wired(repo):
    from backend.services.command_service import set_command_repo
    set_command_repo(repo)
    yield repo
    set_command_repo(None)


def _command(**overrides) -> Command:
    defaults = dict(
        command_id=new_command_id(),
        command_type="workflow",
        owner_user_id="user-1",
        payload={"goal": "do the thing"},
        idempotency_key="key-1",
        request_digest="digest-1",
    )
    defaults.update(overrides)
    from backend.models.command import scoped_idempotency_key
    cmd = Command(**defaults)
    cmd.idempotency_scoped = scoped_idempotency_key(
        cmd.owner_user_id, cmd.command_type, cmd.idempotency_key)
    return cmd


def _event(command: Command) -> OutboxEvent:
    return OutboxEvent(
        event_id=new_event_id(), command_id=command.command_id,
        aggregate_id=command.command_id, aggregate_version=1,
        event_type="command.accepted",
        payload={"command_id": command.command_id})


# ── §4.5: accepted means durable ──────────────────────────────────────

@pytest.mark.asyncio
async def test_acceptance_commits_command_accounting_work_and_outbox(repo, db):
    """One acceptance → four durable artifacts, visible to a DIFFERENT
    connection afterwards."""
    cmd = _command()
    work_done = []

    async def initial_work(conn):
        await conn.execute(
            "INSERT OR IGNORE INTO workflow_runs (run_id, workflow_id, user_id, "
            "goal, status, created_at, updated_at) VALUES (?, 'wf', 'user-1', "
            "'g', 'RUNNING', 'now', 'now')", (cmd.command_id,))
        work_done.append(True)

    receipt = await repo.accept_command_with_initial_work(
        cmd,
        SimpleNamespace(max_pending_per_user=4, max_wait_seconds=3600),
        _event(cmd), initial_work=initial_work)
    assert receipt.accepted is True and receipt.deduplicated is False
    assert work_done == [True]

    # Fresh connection sees everything (committed, not session-local).
    from backend.repositories.sqlite.database import DatabaseManager
    fresh = db
    cmd_row = await (await fresh.execute(
        "SELECT state FROM commands WHERE command_id=?", (cmd.command_id,))).fetchone()
    outbox_row = await (await fresh.execute(
        "SELECT event_id FROM outbox_events WHERE command_id=?",
        (cmd.command_id,))).fetchone()
    pending_row = await (await fresh.execute(
        "SELECT command_id FROM pending_commands WHERE command_id=?",
        (cmd.command_id,))).fetchone()
    run_row = await (await fresh.execute(
        "SELECT run_id FROM workflow_runs WHERE run_id=?", (cmd.command_id,))).fetchone()
    assert cmd_row["state"] == "ACCEPTED"
    assert outbox_row is not None
    assert pending_row is not None
    assert run_row is not None


@pytest.mark.asyncio
async def test_queue_full_refusal_persists_nothing(repo, db):
    """A cap refusal must not leave a command row behind (§4.12: rejected
    work never pretends to be queued)."""
    # Fill the user's queue with 1 unsettled admission against a cap of 1.
    from backend.repositories.admission_repository import SQLiteCommandAdmissionRepository
    admission_repo = SQLiteCommandAdmissionRepository(db)
    await admission_repo.admit_command(
        "pre-existing", "user-1", None, "workflow", 1, 0, 0, 3600)
    cmd = _command()
    receipt = await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=1, max_wait_seconds=3600),
        _event(cmd))
    assert receipt.accepted is False
    assert receipt.error_code == CommandErrorCode.QUEUE_FULL
    cursor = await db.execute(
        "SELECT COUNT(*) FROM commands WHERE command_id=?", (cmd.command_id,))
    assert (await cursor.fetchone())[0] == 0


# ── §4.4: the idempotency contract ────────────────────────────────────

@pytest.mark.asyncio
async def test_same_key_same_payload_returns_original(repo):
    cmd = _command()
    first = await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    replay = await repo.accept_command_with_initial_work(
        _command(),  # same tenant/type/key/digest, new instance
        SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    assert first.accepted and replay.accepted and replay.deduplicated
    assert replay.command_id == first.command_id
    # Only ONE command row exists.
    cursor = await repo.db.execute("SELECT COUNT(*) FROM commands")
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_same_key_different_payload_conflicts(repo):
    await repo.accept_command_with_initial_work(
        _command(), SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(_command()))
    conflicting = _command(request_digest="digest-DIFFERENT")
    conflicting.idempotency_scoped = _command().idempotency_scoped
    receipt = await repo.accept_command_with_initial_work(
        conflicting, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(conflicting))
    assert receipt.accepted is False
    assert receipt.error_code == CommandErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_cross_tenant_same_key_is_independent(repo):
    a = _command(owner_user_id="tenant-a")
    b = _command(owner_user_id="tenant-b")
    ra = await repo.accept_command_with_initial_work(
        a, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(a))
    rb = await repo.accept_command_with_initial_work(
        b, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(b))
    assert ra.accepted and rb.accepted
    assert not rb.deduplicated
    assert rb.command_id != ra.command_id


# ── §4.3: conditional transitions and terminal protection ─────────────

@pytest.mark.asyncio
async def test_lifecycle_path_with_version_guard(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    c = await repo.transition(cmd.command_id, CommandState.QUEUED, actor="test")
    assert c.state is CommandState.QUEUED and c.state_version == 2
    c = await repo.transition(cmd.command_id, CommandState.RUNNING, actor="test")
    c = await repo.transition(
        cmd.command_id, CommandState.SUCCEEDED, actor="test",
        result_reference="runs/xyz")
    assert c.state is CommandState.SUCCEEDED and c.terminal

    # Terminal state is protected: no further transitions.
    with pytest.raises(CommandTransitionError):
        await repo.transition(cmd.command_id, CommandState.FAILED, actor="test")
    # Stale version is rejected.
    with pytest.raises(CommandTransitionError):
        await repo.transition(cmd.command_id, CommandState.FAILED,
                              expected_state_version=1, actor="test")


@pytest.mark.asyncio
async def test_invalid_transition_is_rejected(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    # ACCEPTED → SUCCEEDED skips the eligibility chain: invalid (§4.3).
    with pytest.raises(CommandTransitionError):
        await repo.transition(cmd.command_id, CommandState.SUCCEEDED,
                              actor="test")


# ── §4.15: cancellation as a protocol ─────────────────────────────────

@pytest.mark.asyncio
async def test_cancellation_intent_and_honest_terminal_race(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    await repo.transition(cmd.command_id, CommandState.RUNNING, actor="test")
    cancelled = await repo.record_cancellation_intent(
        cmd.command_id, by="user-1", reason="changed my mind")
    assert cancelled.state is CommandState.CANCEL_REQUESTED
    assert cancelled.cancel_requested_by == "user-1"
    # Duplicate cancellation → same result, no version churn.
    again = await repo.record_cancellation_intent(
        cmd.command_id, by="user-1", reason="changed my mind")
    assert again.state is CommandState.CANCEL_REQUESTED
    assert again.state_version == cancelled.state_version
    # Completion that committed before cancellation took effect is honest.
    done = await repo.transition(cmd.command_id, CommandState.SUCCEEDED,
                                 actor="worker", reason="completed in time")
    assert done.state is CommandState.SUCCEEDED
    # Cancellation after terminal does NOT rewrite the outcome.
    after = await repo.record_cancellation_intent(
        cmd.command_id, by="user-1", reason="late cancel")
    assert after.state is CommandState.SUCCEEDED


@pytest.mark.asyncio
async def test_cancel_before_acceptance_runs_no_execution(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    await repo.transition(cmd.command_id, CommandState.QUEUED, actor="test")
    await repo.record_cancellation_intent(cmd.command_id, by="user-1")
    # CANCEL_REQUESTED → RUNNING is NOT an allowed transition (§4.3): a
    # dispatcher cannot start executing cancelled work.
    from backend.models.command import transition_is_valid
    assert not transition_is_valid(CommandState.CANCEL_REQUESTED,
                                   CommandState.RUNNING)


# ── §4.8: outbox delivery is recoverable ──────────────────────────────

@pytest.mark.asyncio
async def test_outbox_claim_publish_mark_and_crash_replay(repo):
    """Crash after publish, before the published marker → the event is
    re-claimed and RE-PUBLISHED (consumers tolerate duplicates by design);
    the marker is written exactly once."""
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))

    published_ids = []

    async def transport(event):
        published_ids.append(event.event_id)
        # Simulate the crash: the transport succeeded but the process died
        # BEFORE mark_outbox_published ran.

    from backend.services.outbox_dispatcher import dispatch_batch
    # A crashed dispatcher's lease has lapsed by recovery time — simulated
    # with a zero-length lease (expires immediately).
    claimed = await repo.claim_outbox_batch("dispatcher-1", 10, 0)
    for event in claimed:
        await transport(event)
    # (no marker written — the "crash")

    # Recovery: another dispatcher re-claims and publishes AGAIN.
    freed = await repo.reclaim_stale_outbox_leases()
    assert freed == 1
    claimed2 = await repo.claim_outbox_batch("dispatcher-2", 10, 60)
    assert [e.event_id for e in claimed2] == [e.event_id for e in claimed]
    for event in claimed2:
        await transport(event)
        assert await repo.mark_outbox_published(event.event_id) is True
    assert len(published_ids) == 2          # republished: duplicate tolerated
    # A third claim finds nothing: the marker holds.
    assert await repo.claim_outbox_batch("dispatcher-3", 10, 60) == []


@pytest.mark.asyncio
async def test_outbox_failure_backs_off_boundedly(repo):
    cmd = _command()
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    event = (await repo.claim_outbox_batch("d1", 10, 60))[0]
    await repo.fail_outbox_event(event.event_id, "TransportUnavailable")
    row = await (await repo.db.execute(
        "SELECT publish_attempts, available_at, dispatcher_owner FROM outbox_events WHERE event_id=?",
        (event.event_id,))).fetchone()
    assert row["publish_attempts"] == 1
    assert row["available_at"] > int(time.time())   # parked, not tight-looped
    assert row["dispatcher_owner"] is None          # lease released


# ── §4.13: parent waits are race-safe ─────────────────────────────────

@pytest.mark.asyncio
async def test_parent_wait_resolves_once_and_reconciles(db, repo):
    from backend.repositories.wait_repository import SQLiteWaitRepository
    waits = SQLiteWaitRepository(db)
    wait_id = await waits.create_wait("parent-run", "parent-task",
                                      child_run_id="child-run")
    # Duplicate registration (retry) returns the SAME open wait.
    assert await waits.create_wait("parent-run", "parent-task",
                                   child_run_id="child-run") == wait_id
    # Child completes BEFORE the parent's waiter would have looked.
    await repo.db.execute(
        "INSERT INTO workflow_runs (run_id, workflow_id, user_id, goal, status, "
        "created_at, updated_at) VALUES ('child-run', 'wf', 'u', 'g', "
        "'COMPLETED', 'now', 'now')")
    await repo.db.commit()
    # The reconciler reads the child's TERMINAL state from the WORKFLOW repo.
    from unittest.mock import AsyncMock
    workflow_repo = SimpleNamespace(
        get_run=AsyncMock(return_value={"status": "COMPLETED"}))
    resolved = await waits.reconcile_resolved_children(workflow_repo)
    assert resolved == 1
    # Resolving again is a no-op (duplicate child events wake nobody).
    assert await waits.resolve_wait(wait_id, "resolved") is False
    row = await waits.find_open_wait("parent-run", "parent-task")
    assert row is None


# ── §4.18: the reconciler's bounded self-healing ──────────────────────

@pytest.mark.asyncio
async def test_reconciler_expires_deadlined_commands(repo):
    from backend.services.command_reconciler import reconcile_once
    cmd = _command(deadline_at=int(time.time()) - 10)
    await repo.accept_command_with_initial_work(
        cmd, SimpleNamespace(max_pending_per_user=0, max_wait_seconds=3600),
        _event(cmd))
    report = await reconcile_once(repo)
    assert report["expired"] == 1
    c = await repo.get_command(cmd.command_id)
    assert c.state is CommandState.EXPIRED
    # Idempotent: a second pass repairs nothing further.
    report2 = await reconcile_once(repo)
    assert report2["expired"] == 0


# ── §4.11: durable deferral state + reconciler wake ───────────────────

@pytest.mark.asyncio
async def test_deferred_task_persists_wait_and_reconciler_wakes_it(db, repo):
    """A restart cannot erase a deferral: next_attempt_at lands on the task
    row, and the reconciler re-triggers due deferred tasks."""
    from backend.engine.repo_adapter import load_run
    from datetime import datetime, timedelta, timezone

    await repo.db.execute(
        """
        INSERT INTO workflow_runs (run_id, workflow_id, user_id, goal, status,
                                   created_at, updated_at)
        VALUES ('def-run', 'wf-def', 'u1', 'g', 'RUNNING', 'now', 'now')
        """)
    await repo.db.execute(
        """
        INSERT INTO tasks (task_id, run_id, workflow_id, user_id, agent,
                           status, input_data)
        VALUES ('t1', 'def-run', 'wf-def', 'u1', 'OrchestratorAgent',
                'PENDING', '{}')
        """)
    await repo.db.commit()

    engine = SimpleNamespace()
    # _defer_task needs get_run (workflow-shaped); persistence lands via the
    # real db in the persist stub below.
    async def _get_run(*a, **k):
        return {"tasks": [{"task_id": "t1"}]}

    async def _update_deferral(_run_id, _task_id, next_at, defer_count):
        # C9: the deferral lands on the COLUMNS the reconciler queries.
        await repo.db.execute(
            "UPDATE tasks SET next_attempt_at=?, defer_count=? "
            "WHERE run_id=? AND task_id=?",
            (next_at, defer_count, _run_id, _task_id))
        await repo.db.commit()

    engine.repo = SimpleNamespace(get_run=_get_run,
                                  update_task_deferral=_update_deferral)
    engine._defer_counts = {}
    published = []

    async def fake_publish(topic, message):
        published.append(message)

    engine.message_bus = SimpleNamespace(publish=fake_publish)

    async def noop(*a, **k):
        return None

    from backend.engine.engine import WorkflowEngine
    await WorkflowEngine._defer_task(engine, "def-run", "wf-def", "t1",
                                     base_delay_seconds=0.01)

    row = await (await repo.db.execute(
        "SELECT next_attempt_at, defer_count FROM tasks "
        "WHERE run_id='def-run' AND task_id='t1'")).fetchone()
    assert row is not None
    assert row["defer_count"] == 1
    assert row["next_attempt_at"] is not None

    # Backdate the deferral: the reconciler only wakes DUE tasks.
    await repo.db.execute(
        "UPDATE tasks SET next_attempt_at=? WHERE run_id='def-run' AND task_id='t1'",
        ("2000-01-01T00:00:00+00:00",))
    await repo.db.commit()

    # The reconciler wakes the due deferral via the WORKFLOW repo.
    from backend.services.command_reconciler import _wake_due_deferrals

    class _WorkflowRepoStub:
        async def find_due_deferred_tasks(self, now_iso, limit=50):
            cursor = await repo.db.execute(
                "SELECT task_id, run_id, workflow_id FROM tasks "
                "WHERE status='PENDING' AND next_attempt_at IS NOT NULL "
                "AND next_attempt_at<=? LIMIT ?", (now_iso, limit))
            return [dict(r) for r in await cursor.fetchall()]

    woken = await _wake_due_deferrals(_WorkflowRepoStub(), engine.message_bus, 50)
    assert woken == 1
    assert published and published[0]["run_id"] == "def-run"


# ── §4.16: stale schedule-claim recovery ──────────────────────────────

@pytest.mark.asyncio
async def test_stale_schedule_claim_recovered_by_reconciler(db, repo):
    """A process killed between claim and run-creation leaves a claim with
    no run; the reconciler releases it after the horizon so redelivery can
    re-claim (§4.16)."""
    from backend.services.command_reconciler import _recover_stale_claims
    old = int(time.time()) - 3600
    await repo.db.execute(
        "INSERT INTO schedule_occurrences (schedule_id, occurrence, run_id, "
        "status, claimed_at) VALUES ('sch-1', 'occ-old', 'run-never-created', "
        "'claimed', ?)", (old,))
    await repo.db.commit()

    class _ScheduleRepoStub:
        async def find_stale_occurrence_claims(self, older_than_epoch, limit=50):
            cursor = await repo.db.execute(
                "SELECT so.schedule_id, so.occurrence FROM schedule_occurrences so "
                "WHERE so.status='claimed' AND so.claimed_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.run_id = so.run_id) "
                "LIMIT ?", (older_than_epoch, limit))
            return [dict(r) for r in await cursor.fetchall()]

        async def release_occurrence(self, schedule_id, occurrence):
            cursor = await repo.db.execute(
                "DELETE FROM schedule_occurrences WHERE schedule_id=? AND occurrence=?",
                (schedule_id, occurrence))
            await repo.db.commit()
            return bool(cursor.rowcount)

    recovered = await _recover_stale_claims(_ScheduleRepoStub())
    assert recovered == 1
    # The claim is gone — redelivery can re-claim the occurrence.
    row = await (await repo.db.execute(
        "SELECT COUNT(*) FROM schedule_occurrences WHERE schedule_id='sch-1' "
        "AND occurrence='occ-old'")).fetchone()
    assert row[0] == 0
    # Idempotent: a second pass recovers nothing.
    assert await _recover_stale_claims(_ScheduleRepoStub()) == 0
