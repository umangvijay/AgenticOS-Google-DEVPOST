"""
AgentOS — SQLite Workflow Repository

Full CRUD matching the existing WorkflowRepository interface.
Atomic task claiming via SQLite transaction (not get-then-write).
SSE event streaming via polling with last_event_id cursor.
"""

import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone, timedelta

from backend.repositories.base import BaseWorkflowRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)


class SQLiteWorkflowRepository(BaseWorkflowRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    # ── Workflow Runs ─────────────────────────────────────────────

    async def save_run(self, run_data: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = await self.db.connection()

        # Upsert the run
        await conn.execute(
            """
            INSERT INTO workflow_runs (run_id, workflow_id, user_id, goal, status, created_at, updated_at, parent_run_id, thread_id, org_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                run_data["run_id"],
                run_data.get("workflow_id", "default_workflow"),
                run_data.get("user_id", "default_user"),
                run_data["goal"],
                run_data.get("status", "PENDING"),
                run_data.get("created_at", now),
                now,
                run_data.get("parent_run_id"),
                run_data.get("thread_id") or run_data["run_id"],
                run_data.get("org_id"),
            ),
        )

        # Upsert all tasks
        tasks = run_data.get("tasks", [])
        for task in tasks:
            await self._upsert_task(conn, run_data["run_id"], task)

        await conn.commit()

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        run_row = await self.db.fetch_one(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        )
        if not run_row:
            return None

        task_rows = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY rowid", (run_id,)
        )

        result = dict(run_row)
        result["tasks"] = [self._task_row_to_dict(t) for t in task_rows]
        return result


    async def _attach_tasks(self, runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Attach tasks to many runs with ONE query instead of one per run
        (list_runs previously issued N+1 queries — measurable at limit=200)."""
        if not runs:
            return runs
        ids = [r["run_id"] for r in runs]
        placeholders = ",".join("?" for _ in ids)
        rows = await self.db.fetch_all(
            f"SELECT * FROM tasks WHERE run_id IN ({placeholders}) ORDER BY rowid",
            tuple(ids),
        )
        by_run: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            by_run.setdefault(row["run_id"], []).append(self._task_row_to_dict(row))
        for r in runs:
            r["tasks"] = by_run.get(r["run_id"], [])
        return runs

    async def list_runs(self, user_id: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT * FROM workflow_runs
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        )
        return await self._attach_tasks([dict(row) for row in rows])

    async def list_thread_runs(self, user_id: str, thread_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT * FROM workflow_runs
            WHERE user_id = ? AND (thread_id = ? OR run_id = ? OR parent_run_id = ?)
            ORDER BY created_at ASC
            """,
            (user_id, thread_id, thread_id, thread_id),
        )
        return await self._attach_tasks([dict(row) for row in rows])

    async def update_run_status(self, run_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE workflow_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, now, run_id),
        )
        await self.db.commit()

    async def create_if_absent(self, run_data: Dict[str, Any]) -> bool:
        """Idempotent creation. Returns True if created, False if already exists."""
        import aiosqlite
        now = datetime.now(timezone.utc).isoformat()
        # Dedicated connection prevents unrelated coroutines committing this transaction.
        async with aiosqlite.connect(self.db.db_path, timeout=30) as conn:
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    """INSERT OR IGNORE INTO workflow_runs
                    (run_id, workflow_id, user_id, goal, status, created_at, updated_at, parent_run_id, thread_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_data["run_id"], run_data.get("workflow_id", "default_workflow"),
                     run_data.get("user_id", "default_user"), run_data["goal"],
                     run_data.get("status", "PENDING"), run_data.get("created_at", now), now,
                     run_data.get("parent_run_id"), run_data.get("thread_id") or run_data["run_id"]),
                )
                created = cursor.rowcount > 0
                if created:
                    for task in run_data.get("tasks", []):
                        await self._upsert_task(conn, run_data["run_id"], task)
                await conn.commit()
                return created
            except BaseException:
                await conn.rollback()
                raise

    # ── Tasks ─────────────────────────────────────────────────────

    async def save_task(self, task_data: Dict[str, Any]) -> None:
        conn = await self.db.connection()
        await self._upsert_task(conn, task_data["run_id"], task_data)
        await conn.commit()

    async def get_task(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        )
        return self._task_row_to_dict(row) if row else None

    async def cancel_run(self, run_id: str, by: str = "",
                         reason: str = "") -> bool:
        """C6 (§4.15): run-level cancellation intent, idempotent — only
        RUNNING/PENDING runs cancel; PENDING tasks are cancelled with it so
        no descendant work is dispatched afterwards."""
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            "UPDATE workflow_runs SET status='CANCELLED', updated_at=? "
            "WHERE run_id=? AND status IN ('RUNNING','PENDING')",
            (now, run_id))
        if cursor.rowcount:
            await self.db.execute(
                "UPDATE tasks SET status='CANCELLED', completed_at=? "
                "WHERE run_id=? AND status IN ('PENDING','WAITING')",
                (now, run_id))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def update_task_if_not_cancelled(self, run_id: str, task_id: str,
                                           updates: Dict[str, Any], *, expected_attempt=None) -> bool:
        """§4.15 (C6): CONDITIONAL completion — a task whose run was
        cancelled concurrently cannot be overwritten by a stale completion
        (the check-then-write race is closed inside one statement)."""
        if not updates:
            return True
        set_clauses = []
        values = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.extend([run_id, task_id])
        guard = ""
        if expected_attempt is not None:
            guard = " AND attempt=? AND status='RUNNING' AND datetime(lease_expires_at)>datetime('now')"
            values.append(expected_attempt)
        # dict/list values are JSON columns — serialize before binding.
        values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in values]
        cursor = await self.db.execute(
            f"UPDATE tasks SET {', '.join(set_clauses)} "
            "WHERE run_id=? AND task_id=? AND status != 'CANCELLED' "
            "AND EXISTS(SELECT 1 FROM workflow_runs r WHERE r.run_id=tasks.run_id AND r.status!='CANCELLED')"
            + guard,
            values)
        await self.db.commit()
        return cursor.rowcount > 0

    async def update_task_deferral(self, run_id: str, task_id: str,
                                   next_attempt_at: str, defer_count: int) -> None:
        """Phase 4 §4.11 (C9): persist the deferral on the COLUMN the
        reconciler queries — not inside input_data."""
        await self.db.execute(
            "UPDATE tasks SET next_attempt_at=?, defer_count=? "
            "WHERE run_id=? AND task_id=?",
            (next_attempt_at, int(defer_count), run_id, task_id))
        await self.db.commit()

    async def find_due_deferred_tasks(self, now_iso: str, limit: int = 50):
        """Phase 4 §4.11: tasks whose persisted wait has elapsed and which
        have NOT yet been claimed — PENDING deferrals and RETRYING tasks
        whose durable retry timer (§32.6) elapsed without a fast-path
        retrigger."""
        cursor = await self.db.execute(
            """
            SELECT task_id, run_id, workflow_id FROM tasks
            WHERE status IN ('PENDING','RETRYING')
              AND next_attempt_at IS NOT NULL
              AND next_attempt_at<=?
            ORDER BY next_attempt_at LIMIT ?
            """, (now_iso, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def update_task(self, run_id: str, task_id: str, updates: Dict[str, Any]) -> None:
        if not updates:
            return

        set_clauses = []
        values = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            if isinstance(value, (dict, list)):
                values.append(json.dumps(value))
            elif isinstance(value, bool):
                values.append(1 if value else 0)
            else:
                values.append(value)

        values.extend([run_id, task_id])

        conn = await self.db.connection()
        fence = ""
        if "attempt" in updates:
            fence = " AND attempt = ?"
            values.append(updates["attempt"])
        cursor = await conn.execute(
            f"UPDATE tasks SET {', '.join(set_clauses)} WHERE run_id = ? AND task_id = ?{fence}",
            tuple(values),
        )
        await conn.commit()
        if fence and cursor.rowcount == 0:
            from backend.models.exceptions import StaleTaskLeaseError
            raise StaleTaskLeaseError(f"Task {task_id} no longer belongs to this execution")

    async def claim_task(self, run_id: str, task_id: str, lease_seconds: int) -> bool:
        """
        Atomic task claiming using a single conditional UPDATE.
        This is a compare-and-swap: only succeeds if the task is in a claimable state.
        No SELECT-then-UPDATE race condition.
        """
        now = datetime.now(timezone.utc)
        lease_expires = now + timedelta(seconds=lease_seconds)

        conn = await self.db.connection()
        cursor = await conn.execute(
            """
            UPDATE tasks
            SET status = 'RUNNING',
                lease_started_at = ?,
                lease_expires_at = ?,
                attempt = attempt + 1
            WHERE run_id = ? AND task_id = ?
              AND (
                status = 'PENDING'
                OR status = 'RETRYING'
                OR status = 'WAITING'
                OR (status = 'RUNNING' AND lease_expires_at < ?)
              )
            """,
            (
                now.isoformat(),
                lease_expires.isoformat(),
                run_id,
                task_id,
                now.isoformat(),
            ),
        )
        await conn.commit()

        claimed = cursor.rowcount > 0
        if claimed:
            logger.debug(f"Task {task_id} claimed for run {run_id}")
        return claimed

    # ── Approvals ─────────────────────────────────────────────────

    async def save_approval(self, approval_data: Dict[str, Any]) -> None:
        a = approval_data
        await self.db.execute(
            """
            INSERT INTO approvals (
                approval_id, user_id, tool_name, tool_version, risk_level,
                autonomy_level, arguments, arguments_hash, workflow_id,
                run_id, task_id, requested_at, expires_at, requested_by, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a["approval_id"], a["user_id"], a["tool_name"], a["tool_version"],
                a["risk_level"], a["autonomy_level"],
                json.dumps(a.get("arguments", {})), a["arguments_hash"],
                a["workflow_id"], a["run_id"], a["task_id"],
                a["requested_at"], a["expires_at"],
                a.get("requested_by", "system"),
                a.get("status", "PENDING"),
            ),
        )
        await self.db.commit()

    async def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        )
        if not row:
            return None
        result = dict(row)
        result["arguments"] = json.loads(result.get("arguments", "{}"))
        return result

    async def list_pending_approvals(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approvals WHERE user_id = ? AND status = 'PENDING' ORDER BY requested_at DESC",
            (user_id,),
        )
        results = []
        for row in rows:
            r = dict(row)
            r["arguments"] = json.loads(r.get("arguments", "{}"))
            results.append(r)
        return results

    async def resolve_approval(self, approval_id: str, new_status: str, decision_by: str) -> bool:
        """Atomic compare-and-set: only transitions from PENDING."""
        now = datetime.now(timezone.utc).isoformat()
        conn = await self.db.connection()
        cursor = await conn.execute(
            """
            UPDATE approvals
            SET status = ?, decision_by = ?, decision_at = ?
            WHERE approval_id = ? AND status = 'PENDING'
            """,
            (new_status, decision_by, now, approval_id),
        )
        await conn.commit()
        return cursor.rowcount > 0

    # ── Events (SSE Timeline) ─────────────────────────────────────

    async def save_event(self, event_data: Dict[str, Any]) -> None:
        await self.db.execute(
            """
            INSERT INTO events (
                event_id, timestamp, type, workflow_id, run_id,
                task_id, status, summary, sanitized_metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_data["event_id"],
                event_data["timestamp"],
                event_data["type"],
                event_data["workflow_id"],
                event_data["run_id"],
                event_data.get("task_id"),
                event_data.get("status"),
                event_data["summary"],
                json.dumps(event_data.get("sanitized_metadata", {})),
            ),
        )
        await self.db.commit()

    async def get_events(self, run_id: str, after_event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        SSE reconnection support: get events after a specific event_id.
        SQLite has no LISTEN/NOTIFY, so SSE uses polling with this cursor.
        """
        if after_event_id:
            # Get the rowid of the cursor event
            cursor_row = await self.db.fetch_one(
                "SELECT rowid FROM events WHERE event_id = ?", (after_event_id,)
            )
            if cursor_row:
                rows = await self.db.fetch_all(
                    """
                    SELECT * FROM events
                    WHERE run_id = ? AND rowid > ?
                    ORDER BY rowid ASC
                    """,
                    (run_id, cursor_row["rowid"]),
                )
            else:
                rows = await self.db.fetch_all(
                    "SELECT * FROM events WHERE run_id = ? ORDER BY rowid ASC",
                    (run_id,),
                )
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM events WHERE run_id = ? ORDER BY rowid ASC",
                (run_id,),
            )

        results = []
        for row in rows:
            r = dict(row)
            r["sanitized_metadata"] = json.loads(r.get("sanitized_metadata", "{}"))
            results.append(r)
        return results

    # ── Private Helpers ───────────────────────────────────────────

    async def _upsert_task(self, conn, run_id: str, task: Dict[str, Any]) -> None:
        """Insert or update a task."""
        await conn.execute(
            """
            INSERT INTO tasks (
                task_id, run_id, workflow_id, user_id, agent, tool, title, status,
                input_data, output_data, dependencies, started_at, completed_at,
                lease_started_at, lease_expires_at, attempt, timeout_seconds,
                max_retries, recovery_enabled, max_recoveries, max_total_attempts,
                recovery_attempts, original_input, recovery_history,
                error, error_type, trace_id,
                acceptance_criteria_json, requested_outcomes_json,
                allowed_action_scope_json, completion_policy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, task_id) DO UPDATE SET
                status = excluded.status,
                input_data = excluded.input_data,
                output_data = excluded.output_data,
                started_at = excluded.started_at,
                completed_at = excluded.completed_at,
                lease_started_at = excluded.lease_started_at,
                lease_expires_at = excluded.lease_expires_at,
                attempt = excluded.attempt,
                recovery_attempts = excluded.recovery_attempts,
                recovery_history = excluded.recovery_history,
                error = excluded.error,
                error_type = excluded.error_type,
                trace_id = excluded.trace_id,
                acceptance_criteria_json = excluded.acceptance_criteria_json,
                requested_outcomes_json = excluded.requested_outcomes_json,
                allowed_action_scope_json = excluded.allowed_action_scope_json,
                completion_policy = excluded.completion_policy
            """,
            (
                task["task_id"],
                run_id,
                task.get("workflow_id", "default_workflow"),
                task.get("user_id", "default_user"),
                task["agent"],
                task.get("tool"),
                task.get("title"),
                task.get("status", "PENDING"),
                json.dumps(task.get("input_data", {})),
                json.dumps(task.get("output_data")) if task.get("output_data") else None,
                json.dumps(task.get("dependencies", [])),
                task.get("started_at"),
                task.get("completed_at"),
                task.get("lease_started_at"),
                task.get("lease_expires_at"),
                task.get("attempt", 0),
                task.get("timeout_seconds", 60),
                task.get("max_retries", 3),
                1 if task.get("recovery_enabled", False) else 0,
                task.get("max_recoveries", 3),
                task.get("max_total_attempts", 5),
                task.get("recovery_attempts", 0),
                json.dumps(task.get("original_input")) if task.get("original_input") else None,
                json.dumps(task.get("recovery_history", [])),
                task.get("error"),
                task.get("error_type"),
                task.get("trace_id"),
                # §10.7/§11.0: acceptance criteria survive persistence
                json.dumps(task.get("acceptance_criteria", [])
                           if not isinstance(task.get("acceptance_criteria"), str)
                           else task.get("acceptance_criteria")),
                json.dumps(task.get("requested_outcomes", [])
                           if not isinstance(task.get("requested_outcomes"), str)
                           else task.get("requested_outcomes")),
                json.dumps(task.get("allowed_action_scope", [])
                           if not isinstance(task.get("allowed_action_scope"), str)
                           else task.get("allowed_action_scope")),
                task.get("completion_policy", "criteria"),
            ),
        )

    @staticmethod
    def _task_row_to_dict(row: dict) -> Optional[Dict[str, Any]]:
        """Convert a task row to a dict, deserializing JSON fields."""
        if not row:
            return None
        result = dict(row)
        result["input_data"] = json.loads(result.get("input_data", "{}"))
        result["output_data"] = json.loads(result["output_data"]) if result.get("output_data") else None
        result["dependencies"] = json.loads(result.get("dependencies", "[]"))
        result["recovery_history"] = json.loads(result.get("recovery_history", "[]"))
        # §11.0 Finding 3: decode acceptance criteria for reload
        for key in ("acceptance_criteria", "requested_outcomes",
                    "allowed_action_scope"):
            col = f"{key}_json"
            raw = result.get(col)
            result[key] = json.loads(raw) if raw else []
        result["completion_policy"] = result.get("completion_policy", "criteria")
        result["original_input"] = json.loads(result["original_input"]) if result.get("original_input") else None
        result["recovery_enabled"] = bool(result.get("recovery_enabled", 0))
        return result

    async def purge_old_events(self, days: int) -> int:
        """Retention control: delete timeline events older than N days (runs kept)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        conn = await self.db.connection()
        cursor = await conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
        await conn.commit()
        return cursor.rowcount or 0
