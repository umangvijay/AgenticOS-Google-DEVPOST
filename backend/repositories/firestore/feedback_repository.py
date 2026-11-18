"""Feedback lifecycle for the Firestore profile; no training/reward side effects."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
import uuid
import asyncio

from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from backend.repositories.firestore.database import FirestoreDB


class FirestoreFeedbackRepository:
    CATEGORIES = ("incorrect_result", "failed_action", "slow_run",
                  "missing_app", "confusing_ui", "other")
    TRIAGE = ("received", "investigating", "planned", "fixed", "closed")
    RETENTION_BATCH_SIZE = 200

    def __init__(self, client=None):
        self._client = client

    async def _get_db(self):
        return self._client if self._client is not None else await FirestoreDB.get_client()

    async def _collection(self):
        return (await self._get_db()).collection("feedback")

    async def submit(self, feedback: dict) -> str:
        if feedback.get("category") not in self.CATEGORIES:
            raise ValueError(f"category must be one of {self.CATEGORIES}")
        if not isinstance(feedback.get("helpful"), bool):
            raise ValueError("helpful must be a boolean")
        if not feedback.get("user_id"):
            raise ValueError("feedback requires an owner")
        fid = f"fb-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        row = {name: feedback.get(name) for name in (
            "user_id", "helpful", "category", "run_id", "task_id", "tool_id",
            "deliverable_revision", "model_version", "policy_version")}
        row.update(id=fid, comment=(feedback.get("comment") or "")[:4000],
                   expected_result=(feedback.get("expected_result") or "")[:2000],
                   app_version=feedback.get("app_version", ""),
                   share_diagnostics=bool(feedback.get("share_diagnostics", False)),
                   triage_status="received", triage_actor=None, triage_note="",
                   created_at=now, updated_at=now)
        # Ignore caller-supplied status/timestamps and refuse an accidental ID collision.
        await (await self._collection()).document(fid).create(row)
        return fid

    async def get(self, feedback_id: str) -> Optional[dict]:
        snapshot = await (await self._collection()).document(feedback_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def list_for_user(self, user_id: str, limit: int = 50, *, run_id: Optional[str] = None) -> list:
        if limit <= 0:
            return []
        query = (await self._collection()).where(filter=FieldFilter("user_id", "==", user_id))
        if run_id is not None:
            query = query.where(filter=FieldFilter("run_id", "==", run_id))
        query = query.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit)
        return [snapshot.to_dict() async for snapshot in query.stream()]

    async def list_for_triage(self, status_filter: Optional[str] = None, limit: int = 100) -> list:
        if limit <= 0:
            return []
        query = await self._collection()
        if status_filter:
            query = query.where(filter=FieldFilter("triage_status", "==", status_filter))
        query = query.order_by("created_at").limit(limit)
        return [snapshot.to_dict() async for snapshot in query.stream()]

    async def set_triage(self, feedback_id: str, triage_status: str, actor: str, note: str = "") -> bool:
        if triage_status not in self.TRIAGE:
            raise ValueError(f"triage status must be one of {self.TRIAGE}")
        ref = (await self._collection()).document(feedback_id)
        try:
            # update requires an existing document; unlike set, it cannot
            # resurrect a report concurrently deleted by its owner.
            await ref.update({"triage_status": triage_status, "triage_actor": actor,
                              "triage_note": note[:2000], "updated_at": datetime.now(timezone.utc)})
        except NotFound:
            return False
        return True

    async def _delete_version(self, snapshot):
        option = (await self._get_db()).write_option(last_update_time=snapshot.update_time)
        try:
            await snapshot.reference.delete(option=option)
        except (FailedPrecondition, NotFound):
            return False
        return True

    async def delete_user_feedback(self, user_id: str, feedback_id: str) -> bool:
        ref = (await self._collection()).document(feedback_id)
        for attempt in range(8):
            snapshot = await ref.get()
            if not snapshot.exists or snapshot.to_dict().get("user_id") != user_id:
                return False
            if await self._delete_version(snapshot):
                return True
            # Recheck ownership after any concurrent update. Retries are bounded;
            # persistent contention fails explicitly instead of pretending success.
            await asyncio.sleep(min(0.01 * (2 ** attempt), 0.25))
        raise RuntimeError("feedback deletion conflicted repeatedly; retry the operation")

    async def apply_retention(self, retention_days: int) -> int:
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        collection = await self._collection()
        query = collection.where(filter=FieldFilter("created_at", "<", cutoff)).order_by(
            "created_at").limit(self.RETENTION_BATCH_SIZE)
        removed = 0
        stalled_pages = 0
        while True:
            snapshots = [snapshot async for snapshot in query.stream()]
            if not snapshots:
                return removed
            deleted = 0
            for snapshot in snapshots:
                # A concurrent edit/delete invalidates this version; a later
                # query re-evaluates the cutoff. No transaction spans the page.
                deleted += int(await self._delete_version(snapshot))
            removed += deleted
            stalled_pages = stalled_pages + 1 if deleted == 0 else 0
            if stalled_pages >= 8:
                raise RuntimeError("feedback retention repeatedly conflicted; retry the operation")
