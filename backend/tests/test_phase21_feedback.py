"""Phase 21 §21.21: real-user feedback — submission with traceability +
explicit diagnostic consent; owner/admin authorization; triage; user
deletion; the sqlite-backed repo (the local profile) and the postgres
inventory (migration 0002) both implement the contract. Feedback NEVER
feeds a training reward here."""
import uuid

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def fb_stack(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.database import SCHEMA_SQL
    db = DatabaseManager(str(tmp_path / "fb21.db"))
    await db.initialize()
    # The feedback table on the sqlite profile (the durable §21.21 row).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            helpful INTEGER NOT NULL, category TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            expected_result TEXT NOT NULL DEFAULT '',
            run_id TEXT, task_id TEXT, tool_id TEXT,
            deliverable_revision TEXT, model_version TEXT,
            policy_version TEXT, app_version TEXT NOT NULL DEFAULT '',
            share_diagnostics INTEGER NOT NULL DEFAULT 0,
            triage_status TEXT NOT NULL DEFAULT 'received',
            triage_actor TEXT, triage_note TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')
    """)
    from backend.repositories.sqlite.feedback_repository import (
        SQLiteFeedbackRepository)
    repo = SQLiteFeedbackRepository(db)
    yield {"db": db, "repo": repo}
    await db.close()


async def test_submit_triage_and_user_visibility(fb_stack):
    repo = fb_stack["repo"]
    uid = f"u-{uuid.uuid4().hex[:6]}"
    fid = await repo.submit({
        "user_id": uid, "helpful": False, "category": "incorrect_result",
        "comment": "The summary mixed two runs",
        "expected_result": "Only run A's data",
        "run_id": "run-123", "task_id": "t1",
        "share_diagnostics": True, "app_version": "21.0",
    })
    assert fid
    # The user sees the triage status of their own report.
    mine = await repo.list_for_user(uid)
    assert mine and mine[0]["triage_status"] == "received"
    # Authorized triage moves it through the states.
    assert await repo.set_triage(fid, "investigating", "staff-1",
                                 "reproduced with the run export")
    assert await repo.set_triage(fid, "fixed", "staff-1", "shipped")
    mine2 = await repo.list_for_user(uid)
    assert mine2[0]["triage_status"] == "fixed"


async def test_invalid_category_and_helpful_refused(fb_stack):
    repo = fb_stack["repo"]
    with pytest.raises(ValueError):
        await repo.submit({"user_id": "u1", "helpful": True,
                           "category": "not-a-category"})
    with pytest.raises(ValueError):
        await repo.submit({"user_id": "u1", "helpful": "yes",
                           "category": "other"})


async def test_user_deletion_and_retention(fb_stack):
    repo = fb_stack["repo"]
    uid = f"u-{uuid.uuid4().hex[:6]}"
    fid = await repo.submit({"user_id": uid, "helpful": True,
                             "category": "slow_run",
                             "comment": "took 40s to plan"})
    assert await repo.delete_user_feedback(uid, fid)
    assert await repo.list_for_user(uid) == []
    # A DIFFERENT user cannot delete another's feedback.
    fid2 = await repo.submit({"user_id": "owner-x", "helpful": True,
                              "category": "other"})
    assert not await repo.delete_user_feedback("attacker", fid2)
    # Retention: old rows are purged by the operator job.
    old_fid = await repo.submit({"user_id": "owner-x", "helpful": False,
                                 "category": "other"})
    await repo.db.execute(
        "UPDATE feedback SET created_at='2020-01-01T00:00:00+00:00' "
        "WHERE id=?", (old_fid,))
    await repo.db.commit()
    purged = await repo.apply_retention(retention_days=365)
    assert purged >= 1
