"""
AgentOS — Round 3 feature regression tests

Covers the audit P1/P2 additions: outbound webhook signing, scheduled-run
factory determinism, shared task titles, and the token denylist store.
"""

import json
import pytest

from backend.security.webhook_signature import verify_signature
from backend.services.run_webhooks import sign_payload
from backend.scheduler.run_factory import build_scheduled_run, scheduled_run_id
from backend.engine.task_titles import task_title


def test_webhook_signature_roundtrip_matches_inbound_verification():
    import time
    secret = "s3cret"
    body = json.dumps({"run_id": "run-1", "status": "COMPLETED"}).encode()
    ts = str(int(time.time()))  # verify_signature enforces ±300s freshness
    signature = sign_payload(secret, ts, body)
    assert verify_signature(secret, ts, signature, body)
    assert not verify_signature("wrong", ts, signature, body)
    assert not verify_signature(secret, str(int(time.time()) + 301), signature, body)


def test_scheduled_run_id_is_deterministic_per_window():
    from datetime import datetime, timezone
    t = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    assert scheduled_run_id("sch-1", t) == scheduled_run_id("sch-1", t)
    assert scheduled_run_id("sch-1", t) != scheduled_run_id("sch-2", t)
    assert scheduled_run_id("sch-1", t).startswith("sch-")


def test_build_scheduled_run_has_goal_tasks_and_title():
    schedule = {"user_id": "u1", "goal": "check the health of https://example.com", "name": "watchdog"}
    run = build_scheduled_run(schedule, scheduled_run_id("s", __import__("datetime").datetime.now(__import__("datetime").timezone.utc)))
    assert run.tasks, "scheduled run must have at least one task"
    assert all(t.user_id == "u1" for t in run.tasks)
    assert all(t.title for t in run.tasks), "every scheduled task carries a title"


def test_task_title_prefers_meaningful_input():
    class Def:
        tool = "core.health"
        agent = "core.health"
        input_data = {"url": "https://example.com"}
    assert task_title(Def()) == "core.health: https://example.com"

    class Bare:
        tool = None
        agent = "OrchestratorAgent"
        input_data = {}
    assert task_title(Bare()) == "OrchestratorAgent"


@pytest.mark.asyncio
async def test_token_denylist_revoke_and_expire(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.token_denylist_repository import SQLiteTokenDenylistRepository
    from datetime import datetime, timezone, timedelta

    db = DatabaseManager(str(tmp_path / "test.db"))
    await db.initialize()
    repo = SQLiteTokenDenylistRepository(db)

    await repo.revoke_jti("jti-live", datetime.now(timezone.utc) + timedelta(minutes=10))
    await repo.revoke_jti("jti-dead", datetime.now(timezone.utc) - timedelta(minutes=1))

    assert await repo.is_revoked("jti-live") is True
    assert await repo.is_revoked("jti-dead") is False
    assert await repo.is_revoked("unknown") is False

    purged = await repo.purge_expired()
    assert purged >= 1
    await db.close()
