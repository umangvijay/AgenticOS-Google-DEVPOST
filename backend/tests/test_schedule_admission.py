"""Phase 3 §3.21: atomic schedule admission (§3.15).

The read-then-start check is replaced by an atomic occurrence claim
(schedule_id + occurrence PRIMARY KEY). Under test: duplicate deliveries
resolve to ONE logical execution, the loser is explicitly skipped, a crash
between claim and run-creation releases the claim for redelivery, and
independent connections race the same claim with a single winner.
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.schedule_repository import SQLiteScheduleRepository


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "schedules.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return SQLiteScheduleRepository(db)


@pytest.mark.asyncio
async def test_duplicate_occurrence_claim_yields_one_winner(repo):
    """§3.21 'Duplicate schedule occurrence → one logical execution'."""
    occurrence = "2026-10-31T12:00:00+00:00"
    assert await repo.claim_occurrence("sched-1", occurrence, "run-1") is True
    # Redelivery / second worker: claim refused, run NOT duplicated.
    assert await repo.claim_occurrence("sched-1", occurrence, "run-2") is False
    assert await repo.claim_occurrence("sched-1", occurrence, "run-3") is False


@pytest.mark.asyncio
async def test_different_occurrences_claim_independently(repo):
    assert await repo.claim_occurrence("sched-1", "2026-10-31T12:00:00+00:00",
                                       "run-1") is True
    assert await repo.claim_occurrence("sched-1", "2026-10-31T13:00:00+00:00",
                                       "run-2") is True
    assert await repo.claim_occurrence("sched-2", "2026-10-31T12:00:00+00:00",
                                       "run-3") is True


@pytest.mark.asyncio
async def test_released_claim_is_reclaimable(repo):
    """Crash between claim and run-creation: the except path releases the
    claim so the redelivered trigger can re-claim and run."""
    occurrence = "2026-10-31T14:00:00+00:00"
    assert await repo.claim_occurrence("sched-9", occurrence, "run-1") is True
    assert await repo.release_occurrence("sched-9", occurrence) is True
    assert await repo.claim_occurrence("sched-9", occurrence, "run-1b") is True


@pytest.mark.asyncio
async def test_finish_occurrence_records_terminal_state(repo):
    occurrence = "2026-10-31T15:00:00+00:00"
    await repo.claim_occurrence("sched-2", occurrence, "run-1")
    await repo.finish_occurrence("sched-2", occurrence, "skipped_busy")
    rows = await repo.db.fetch_all(
        "SELECT status FROM schedule_occurrences WHERE schedule_id=? AND occurrence=?",
        ("sched-2", occurrence))
    assert rows[0]["status"] == "skipped_busy"
    # A finished occurrence is still claimed — finishing is bookkeeping, not release.
    assert await repo.claim_occurrence("sched-2", occurrence, "run-2") is False


@pytest.mark.asyncio
async def test_independent_connections_race_one_claim(tmp_path):
    """§3.21 multi-instance evidence: two connections (two 'workers') race
    the same occurrence — exactly one wins."""
    from backend.repositories.sqlite.database import DatabaseManager
    path = str(tmp_path / "race.db")
    db_a = DatabaseManager(path)
    db_b = DatabaseManager(path)
    await db_a.initialize()
    try:
        await db_b.initialize()
        repo_a = SQLiteScheduleRepository(db_a)
        repo_b = SQLiteScheduleRepository(db_b)
        first = await repo_a.claim_occurrence("sched-r", "occ-1", "run-a")
        second = await repo_b.claim_occurrence("sched-r", "occ-1", "run-b")
        assert [first, second] == [True, False]
    finally:
        await db_a.close()
        await db_b.close()


@pytest.mark.asyncio
async def test_worker_duplicate_trigger_handler_skips(db, repo, monkeypatch, caplog):
    """The worker's handle_schedule honors the claim: a duplicate trigger
    acks WITHOUT creating a second run."""
    from datetime import datetime, timezone
    from backend.worker import start_worker
    from backend.repositories.message_bus import MessageContext

    class _AckCtx(MessageContext):
        def __init__(self):
            self.acked = False
            self.nacked = False

        async def ack(self):
            self.acked = True

        async def nack(self):
            self.nacked = True

    captured = {}

    async def fake_consume(topic, handler):
        captured[topic] = handler
        raise RuntimeError("stop-consume")   # end start_worker early

    class _Bus:
        consume = staticmethod(fake_consume)

    class _Repo:
        async def get_run(self, run_id):
            return None

    from backend.engine.engine import WorkflowEngine
    engine = WorkflowEngine.__new__(WorkflowEngine)
    engine.repo = _Repo()

    monkeypatch.setattr("backend.worker.start_worker", start_worker)
    schedule_repo = type("R", (), {})()   # minimal: claim + history
    schedule_repo.claim_occurrence = repo.claim_occurrence.__get__(schedule_repo)
    schedule_repo.get_schedule = lambda *a, **k: _async({"schedule_id": "s1", "status": "ACTIVE", "user_id": "u1", "name": "n", "goal": "g", "cron_expression": "* * * * *"})
    schedule_repo.get_execution_history = lambda *a, **k: _async([])
    schedule_repo.finish_occurrence = repo.finish_occurrence.__get__(schedule_repo)

    async def _async(value):
        return value

    try:
        await start_worker(_Bus(), engine, schedule_repo)
    except RuntimeError as exc:
        assert "stop-consume" in str(exc)
    handler = captured["agentos-scheduler-triggers"]

    from backend.models.schedule import SchedulerTriggerEvent
    occurrence_dt = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    # First delivery claims and proceeds (fails later at run creation — repo
    # is a stub — but the claim must already exist).
    ctx1 = _AckCtx()
    await handler(SchedulerTriggerEvent(schedule_id="s1", scheduled_time=occurrence_dt).model_dump(mode="json"), ctx1)
    ctx2 = _AckCtx()
    await handler(SchedulerTriggerEvent(schedule_id="s1", scheduled_time=occurrence_dt).model_dump(mode="json"), ctx2)
    # Second delivery hits the existing claim and is acked as a duplicate skip.
    assert ctx2.acked is True and ctx2.nacked is False
