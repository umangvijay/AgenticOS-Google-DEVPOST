"""Parent-wait repository (Phase 4 §4.13).

Durable dependency records between a parent run/task and child work (a
queued integration build). The wait is established BEFORE the child is
exposed to execution — ideally in the acceptance transaction — so the race
where the child completes before the parent registered its waiting state
cannot strand the parent asleep forever:

    create_wait → (child runs) → child terminal event → resolve_wait:
    a CONDITIONAL transition (only one resolver wins), then the parent task
    is woken exactly once. Duplicate child events cannot wake twice (the
    open-wait unique index + resolved state). A reconciler sweeps terminal
    children whose waits are still unresolved.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Protocol

from backend.models.admission import epoch_now
from backend.models.command import new_wait_id

logger = logging.getLogger(__name__)


class WaitRepository(Protocol):
    async def create_wait(self, parent_run_id: str, parent_task_id: str,
                          child_run_id: Optional[str] = None,
                          child_command_id: Optional[str] = None,
                          condition: str = "child_build_succeeded",
                          parent_command_id: Optional[str] = None) -> str: ...

    async def resolve_wait(self, wait_id: str, outcome: str,
                           by_event_id: Optional[str] = None) -> bool: ...

    async def find_open_wait(self, parent_run_id: str,
                             parent_task_id: str) -> Optional[Dict[str, Any]]: ...

    async def unresolved_waits_for_child(self, child_run_id: str) -> list: ...

    async def latest_wait(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]: ...

    async def resolve_with_wake(self, wait: Dict[str, Any], payload: Dict[str, Any],
                                *, outcome: str = "resolved") -> bool: ...

    async def reconcile_resolved_children(self, repo, limit: int = 50) -> int: ...


class SQLiteWaitRepository:
    def __init__(self, db):
        self.db = db

    async def create_wait(self, parent_run_id: str, parent_task_id: str,
                          child_run_id: Optional[str] = None,
                          child_command_id: Optional[str] = None,
                          condition: str = "child_build_succeeded",
                          parent_command_id: Optional[str] = None) -> str:
        now = epoch_now()
        wait_id = new_wait_id()
        try:
            await self.db.execute(
                "INSERT INTO parent_waits (wait_id, parent_command_id, "
                "parent_run_id, parent_task_id, child_command_id, child_run_id, "
                "condition, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?)",
                (wait_id, parent_command_id, parent_run_id, parent_task_id,
                 child_command_id, child_run_id, condition, now))
            await self.db.commit()
            return wait_id
        except Exception:
            # The open-wait unique index fired: this parent task already has
            # exactly ONE open wait — idempotent re-registration returns it.
            await self.db.commit()
            existing = await self.find_open_wait(parent_run_id, parent_task_id)
            if existing:
                return existing["wait_id"]
            raise

    async def resolve_wait(self, wait_id: str, outcome: str,
                           by_event_id: Optional[str] = None) -> bool:
        """§4.13: conditional resolution — only a WAITING record resolves,
        exactly once (duplicate child events cannot wake the parent twice)."""
        cursor = await self.db.execute(
            "UPDATE parent_waits SET state=?, resolved_by_event_id=?, resolved_at=? "
            "WHERE wait_id=? AND state='waiting'",
            (outcome, by_event_id, epoch_now(), wait_id))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def find_open_wait(self, parent_run_id: str,
                             parent_task_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM parent_waits WHERE parent_run_id=? AND parent_task_id=? "
            "AND state='waiting'", (parent_run_id, parent_task_id))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def unresolved_waits_for_child(self, child_run_id: str) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM parent_waits WHERE child_run_id=? AND state='waiting'",
            (child_run_id,))
        return [dict(r) for r in await cursor.fetchall()]

    async def unresolved_waits(self, limit: int = 50) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM parent_waits WHERE state='waiting' LIMIT ?",
            (limit,))
        return [dict(r) for r in await cursor.fetchall()]

    async def latest_wait(self, run_id, task_id):
        row = await self.db.fetch_one("SELECT * FROM parent_waits WHERE parent_run_id=? AND parent_task_id=? "
                                      "ORDER BY created_at DESC LIMIT 1", (run_id, task_id))
        return dict(row) if row else None

    async def resolve_with_wake(self, wait, payload, *, outcome="resolved"):
        if outcome not in ("resolved", "failed"):
            raise ValueError("Unsupported parent-wait outcome")
        import json
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute("UPDATE parent_waits SET state=?,resolved_at=? "
                                        "WHERE wait_id=? AND state='waiting'", (outcome, now, wait["wait_id"]))
            if not cursor.rowcount:
                return False
            await conn.execute("INSERT INTO outbox_events(event_id,aggregate_id,aggregate_version,event_type,"
                               "payload_json,created_at,available_at) VALUES(?,?,1,'parent.ready',?,?,?)",
                               ("wake-" + wait["wait_id"], wait["parent_run_id"], json.dumps(payload), now, now))
        return True

    async def reconcile_resolved_children(self, repo, limit: int = 50) -> int:
        """§4.18 row: 'Completed child with unresolved parent wait' — the
        child's terminal state is the durable fact; the wait resolves and
        the parent wakes once."""
        from backend.engine.repo_adapter import maybe_await
        waits = await self.unresolved_waits(limit)
        resolved = 0
        for wait in waits:
            child_run_id = wait.get("child_run_id")
            if not child_run_id:
                continue
            child = await maybe_await(repo.get_run(child_run_id))
            if not child or child.get("status") not in ("COMPLETED", "FAILED", "CANCELLED"):
                continue
            outcome = ("resolved" if child.get("status") == "COMPLETED"
                       else "failed")
            if await self.resolve_wait(wait["wait_id"], outcome):
                resolved += 1
        return resolved


_repo: Optional[WaitRepository] = None


def set_wait_repo(repo: Optional[WaitRepository]) -> None:
    global _repo
    _repo = repo


def get_wait_repo() -> Optional[WaitRepository]:
    return _repo
