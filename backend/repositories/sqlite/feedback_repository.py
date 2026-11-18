"""§21.21 feedback repository — the sqlite profile (local-first)."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

class SQLiteFeedbackRepository:
    CATEGORIES = ("incorrect_result", "failed_action", "slow_run",
                  "missing_app", "confusing_ui", "other")
    TRIAGE = ("received", "investigating", "planned", "fixed", "closed")

    def __init__(self, db):
        self.db = db

    async def submit(self, feedback: Dict[str, Any]) -> str:
        if feedback.get("category") not in self.CATEGORIES:
            raise ValueError(f"category must be one of {self.CATEGORIES}")
        if not isinstance(feedback.get("helpful"), bool):
            raise ValueError("helpful must be a boolean")
        fid = f"fb-{uuid.uuid4().hex[:14]}"
        await self.db.execute(
            """INSERT INTO feedback (id, user_id, helpful, category,
                 comment, expected_result, run_id, task_id, tool_id,
                 deliverable_revision, model_version, policy_version,
                 app_version, share_diagnostics, triage_status, created_at,
                 updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'received', ?, ?)""",
            (fid, feedback["user_id"], int(feedback["helpful"]),
             feedback["category"], (feedback.get("comment") or "")[:4000],
             (feedback.get("expected_result") or "")[:2000],
             feedback.get("run_id"), feedback.get("task_id"),
             feedback.get("tool_id"), feedback.get("deliverable_revision"),
             feedback.get("model_version"), feedback.get("policy_version"),
             feedback.get("app_version", ""),
             int(bool(feedback.get("share_diagnostics", False))),
             time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             time.strftime("%Y-%m-%dT%H:%M:%S%z")))
        await self.db.commit()
        return fid

    async def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            "SELECT * FROM feedback WHERE id=?", (feedback_id,))

    async def list_for_user(self, user_id: str, limit: int = 50, *, run_id: Optional[str] = None) -> list:
        if run_id is not None:
            return await self.db.fetch_all(
                "SELECT * FROM feedback WHERE user_id=? AND run_id=? "
                "ORDER BY created_at DESC LIMIT ?", (user_id, run_id, limit))
        return await self.db.fetch_all(
            "SELECT * FROM feedback WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT ?", (user_id, limit))

    async def list_for_triage(self, status_filter: Optional[str] = None,
                              limit: int = 100) -> list:
        if status_filter:
            return await self.db.fetch_all(
                "SELECT * FROM feedback WHERE triage_status=? "
                "ORDER BY created_at LIMIT ?", (status_filter, limit))
        return await self.db.fetch_all(
            "SELECT * FROM feedback ORDER BY created_at LIMIT ?", (limit,))

    async def set_triage(self, feedback_id: str, triage_status: str,
                         actor: str, note: str = "") -> bool:
        if triage_status not in self.TRIAGE:
            raise ValueError(f"triage status must be one of {self.TRIAGE}")
        cur = await self.db.execute(
            "UPDATE feedback SET triage_status=?, triage_actor=?, "
            "triage_note=?, updated_at=? WHERE id=?",
            (triage_status, actor, note[:2000],
             time.strftime("%Y-%m-%dT%H:%M:%S%z"), feedback_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def delete_user_feedback(self, user_id: str,
                                   feedback_id: str) -> bool:
        cur = await self.db.execute(
            "DELETE FROM feedback WHERE user_id=? AND id=?",
            (user_id, feedback_id))
        await self.db.commit()
        return cur.rowcount > 0

    async def apply_retention(self, retention_days: int) -> int:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z",
            time.localtime(time.time() - retention_days * 86400))
        cur = await self.db.execute(
            "DELETE FROM feedback WHERE created_at < ?", (cutoff,))
        await self.db.commit()
        return cur.rowcount
