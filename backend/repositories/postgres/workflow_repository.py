"""Postgres core-subset repositories (§20.4): workflows/runs, tasks
with durable lease claim, commands (idempotent), events (ordered,
replayable), deliverables (revisioned), and budget reservations."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.repositories.postgres.database import PostgresDatabaseManager
from backend.repositories.postgres.repositories import _json, _parse_json


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class PostgresWorkflowRepository:
    """Runs + tasks. Mirrors the SQLite contract: run/task rows carry
    user ownership; task updates are conditional on lifecycle state."""

    def __init__(self, db: PostgresDatabaseManager):
        self.db = db

    # ── users ─────────────────────────────────────────────────────────
    async def upsert_user(self, user_id: str, email: str,
                          name: str = "", role: str = "user") -> None:
        await self.db.execute(
            """INSERT INTO users (id, email, name, role) VALUES ($1,$2,$3,$4)
               ON CONFLICT (id) DO UPDATE SET email=EXCLUDED.email,
                 name=EXCLUDED.name, role=EXCLUDED.role,
                 updated_at=now()""",
            user_id, email, name, role)

    async def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one("SELECT * FROM users WHERE id=$1",
                                       user_id)

    # ── workflows ─────────────────────────────────────────────────────
    async def create_run(self, run_id: str, user_id: str, goal: str,
                         org_id: Optional[str] = None,
                         create_if_absent: bool = False) -> bool:
        if create_if_absent:
            row = await self.db.fetch_one(
                """INSERT INTO workflows (id, user_id, org_id, goal)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (id) DO NOTHING RETURNING id""",
                run_id, user_id, org_id, goal)
            return row is not None
        await self.db.execute(
            "INSERT INTO workflows (id, user_id, org_id, goal) "
            "VALUES ($1,$2,$3,$4)", run_id, user_id, org_id, goal)
        return True

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """SQLite parity: the run dict EMBEDS its tasks (dict_to_run and
        the DAG evaluator consume run["tasks"])."""
        row = await self.db.fetch_one("SELECT * FROM workflows WHERE id=$1",
                                      run_id)
        if row is None:
            return None
        out = dict(row)
        out["run_id"] = out["id"]
        out.setdefault("workflow_id", f"wf-{out['id'][:8]}")
        rows = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE run_id=$1 ORDER BY task_index", run_id)
        tasks = []
        for r in rows:
            payload = _parse_json(r.get("payload")) or {}
            task = {**payload, "task_id": r["id"], "run_id": r["run_id"],
                    "status": r["status"], "title": r["title"],
                    "task_index": r["task_index"],
                    "lease_owner": r["lease_owner"], "lease_expires_at": r["lease_expires_at"],
                    "attempt": r["attempt"], "error": r["error"]}
            tasks.append(task)
        out["tasks"] = tasks
        return out

    async def update_run_status(self, run_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE workflows SET status=$2, updated_at=now() WHERE id=$1",
            run_id, status)

    async def list_runs(self, user_id: str, limit: int = 50,
                        offset: int = 0) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM workflows WHERE user_id=$1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            user_id, limit, offset)

    # ── tasks (durable lease claim) ───────────────────────────────────
    async def save_task(self, task_data, run_id: str = "", user_id: str = "",
                        title: str = "", task_index: int = 0,
                        payload: Optional[Dict[str, Any]] = None,
                        status: str = "pending") -> None:
        """Dict-based contract (sqlite parity): save_task(task_dict); the
        positional form is kept for direct callers."""
        payload = payload or {}
        if isinstance(task_data, dict):
            rid = task_data.get("run_id") or run_id
            uid = task_data.get("user_id") or user_id
            tid = task_data.get("task_id") or task_data.get("id") or ""
            title = task_data.get("title", title)
            task_index = int(task_data.get("task_index", task_index) or 0)
            status = str(task_data.get("status") or status).upper()
            payload = {k: v for k, v in task_data.items()
                       if k not in ("task_id", "id", "run_id", "user_id",
                                    "title", "task_index", "status")}
        else:
            tid = task_data
            rid, uid = run_id, user_id
        await self.db.execute(
            """INSERT INTO tasks (id, run_id, user_id, task_index, title,
                 status, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
               ON CONFLICT (run_id, id) DO UPDATE SET title=EXCLUDED.title,
                 status=EXCLUDED.status,
                 payload=EXCLUDED.payload, updated_at=now()""",
            tid, rid, uid, task_index, title, status, _json(payload or {}))

    async def get_task(self, run_id_or_task_id, task_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """sqlite parity: get_task(run_id, task_id); the single-arg form
        looks up by task id alone."""
        if task_id is None:
            row = await self.db.fetch_one(
                "SELECT * FROM tasks WHERE id=$1 ORDER BY created_at DESC LIMIT 1",
                run_id_or_task_id)
        else:
            row = await self.db.fetch_one(
                "SELECT * FROM tasks WHERE run_id=$1 AND id=$2",
                run_id_or_task_id, task_id)
        if row and isinstance(row.get("payload"), str):
            row = dict(row)
            row["payload"] = json.loads(row["payload"])
        return row

    async def claim_task(self, run_id: str, task_id: str,
                         lease_seconds: int) -> bool:
        """sqlite parity: claim_task(run_id, task_id, lease) — a CAS that
        claims PENDING/RETRYING/WAITING tasks and EXPIRED leases only,
        sets status RUNNING, and bumps the attempt (durable execution)."""
        row = await self.db.fetch_one(
            """UPDATE tasks SET
                 status='RUNNING',
                 lease_owner=$3,
                 lease_expires_at=now() + make_interval(secs => $4),
                 attempt=attempt+1,
                 payload = payload || jsonb_build_object(
                   'attempt',
                   attempt + 1),
                 updated_at=now()
               WHERE run_id=$1 AND id=$2
                 AND (UPPER(status) IN ('PENDING','RETRYING','WAITING')
                      OR (UPPER(status)='RUNNING' AND
                          (lease_expires_at IS NULL OR lease_expires_at < now())))
               RETURNING id""",
            run_id, task_id, f"worker-{run_id[:8]}:{task_id}", lease_seconds)
        return row is not None

    async def release_task_lease(self, run_id_or_task_id,
                                 task_id: Optional[str] = None) -> None:
        if task_id is None:
            await self.db.execute(
                "UPDATE tasks SET lease_owner=NULL, lease_expires_at=NULL "
                "WHERE id=$1", run_id_or_task_id)
        else:
            await self.db.execute(
                "UPDATE tasks SET lease_owner=NULL, lease_expires_at=NULL "
                "WHERE run_id=$1 AND id=$2", run_id_or_task_id, task_id)

    async def update_task_if_not_terminal(self, task_id: str,
                                          status: str) -> bool:
        row = await self.db.fetch_one(
            """UPDATE tasks SET status=$2, updated_at=now()
               WHERE id=$1 AND status NOT IN ('completed', 'cancelled')
               RETURNING id""", task_id, status)
        return row is not None

    # ── commands (idempotent receipt) ─────────────────────────────────
    async def record_command(self, command_id: str, run_id: str,
                             user_id: str, command_type: str,
                             payload: Optional[Dict[str, Any]] = None,
                             idempotency_key: Optional[str] = None) -> bool:
        row = await self.db.fetch_one(
            """INSERT INTO commands (id, run_id, user_id, command_type,
                 payload, idempotency_key)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
            command_id, run_id, user_id, command_type,
            _json(payload or {}), idempotency_key)
        return row is not None

    # ── events (ordered, replayable) ──────────────────────────────────
    async def append_event(self, run_id: str, user_id: str, event_type: str,
                           payload: Optional[Dict[str, Any]] = None) -> int:
        row = await self.db.fetch_one(
            """INSERT INTO events (run_id, user_id, event_type, payload)
               VALUES ($1,$2,$3,$4::jsonb) RETURNING id""",
            run_id, user_id, event_type, _json(payload or {}))
        return int(row["id"])

    async def get_events(self, run_id: str, after_id: int = 0,
                         after_event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """SSE contract parity with SQLiteWorkflowRepository: the cursor
        is an event_id STRING (`after_event_id`), resolved to the numeric
        row id here — the events endpoint passes `after_event_id=` on
        every backend (Phase 22: the pg profile previously raised
        TypeError, breaking live event streaming)."""
        cursor = after_id
        if after_event_id:
            row = await self.db.fetch_one(
                "SELECT id FROM events WHERE event_id=$1", after_event_id)
            cursor = int(row["id"]) if row else 0
        rows = await self.db.fetch_all(
            "SELECT * FROM events WHERE run_id=$1 AND id > $2 ORDER BY id",
            run_id, cursor)
        for r in rows:
            if isinstance(r.get("payload"), str):
                r["payload"] = json.loads(r["payload"])
        return rows

    # ── deliverables (revisioned) ─────────────────────────────────────
    async def save_deliverable(self, deliverable_id: str, run_id: str,
                               user_id: str, deliverable_type: str,
                               content: Dict[str, Any],
                               content_checksum: str = "") -> int:
        """Insert as revision 1; further saves bump the revision via a
        conditional UPDATE so concurrent writers never overwrite."""
        row = await self.db.fetch_one(
            """INSERT INTO deliverables (id, run_id, user_id,
                 deliverable_type, content, content_checksum)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6)
               ON CONFLICT (id) DO UPDATE SET
                 revision = deliverables.revision + 1,
                 content = EXCLUDED.content,
                 content_checksum = EXCLUDED.content_checksum,
                 updated_at = now()
               RETURNING revision""",
            deliverable_id, run_id, user_id, deliverable_type,
            _json(content), content_checksum)
        return int(row["revision"])

    async def get_deliverable(self, deliverable_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM deliverables WHERE id=$1", deliverable_id)
        if row and isinstance(row.get("content"), str):
            row["content"] = json.loads(row["content"])
        return row


class PostgresBudgetRepository:
    """Reservation-shaped budget accounting on Postgres (§20.4)."""

    def __init__(self, db: PostgresDatabaseManager):
        self.db = db

    async def reserve(self, reservation_id: str, user_id: str,
                      period: str, tokens: int,
                      run_id: Optional[str] = None) -> bool:
        # 0015 made the §2 contract columns NOT NULL — the legacy simple
        # reserve populates them with the reservation identity.
        await self.db.execute(
            """INSERT INTO budget_reservations
                 (id, reservation_id, request_key, request_digest, user_id,
                  run_id, period, reserved, units, settled_units, state)
               VALUES ($1,$1,$1,$1,$2,$3,$4,$5,$5,0,'reserved')""",
            reservation_id, user_id, run_id, period, tokens)
        return True

    async def settle(self, reservation_id: str, used: int) -> bool:
        row = await self.db.fetch_one(
            """UPDATE budget_reservations
               SET settled=$2, state='settled'
               WHERE id=$1 AND state='reserved' RETURNING id""",
            reservation_id, used)
        return row is not None

    async def release(self, reservation_id: str) -> bool:
        row = await self.db.fetch_one(
            """UPDATE budget_reservations SET state='released'
               WHERE id=$1 AND state='reserved' RETURNING id""",
            reservation_id)
        return row is not None

    async def usage_for_period(self, user_id: str, period: str) -> Dict[str, int]:
        row = await self.db.fetch_one(
            """SELECT COALESCE(SUM(reserved),0) AS reserved,
                      COALESCE(SUM(settled),0) AS settled
               FROM budget_reservations
               WHERE user_id=$1 AND period=$2 AND state='settled'""",
            user_id, period)
        return {"reserved": int(row["reserved"]),
                "settled": int(row["settled"])}


class PostgresWorkflowEngineRepository(PostgresWorkflowRepository):
    """Engine-facing surface: full run persistence (runs + tasks as the
    engine's dict shape), conditional task updates, approvals, and
    durable deferral — mirroring SQLiteWorkflowRepository contracts so
    the engine and repo_adapter run unchanged on Postgres."""

    RUN_COLUMNS = ("id, user_id, org_id, goal, status, created_at, updated_at")

    async def save_run(self, run_data: Dict[str, Any]) -> None:
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO workflows (id, user_id, org_id, goal,
                         status) VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (id) DO UPDATE SET goal=EXCLUDED.goal,
                         status=EXCLUDED.status, updated_at=now()""",
                    run_data["run_id"], run_data.get("user_id", ""),
                    run_data.get("org_id"), run_data.get("goal", ""),
                    run_data.get("status", "RUNNING"))
                for task in run_data.get("tasks", []):
                    await conn.execute(
                        """INSERT INTO tasks (id, run_id, user_id,
                             task_index, title, status, payload)
                           VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
                           ON CONFLICT (run_id, id) DO UPDATE SET
                             status=EXCLUDED.status, payload=EXCLUDED.payload,
                             title=EXCLUDED.title, updated_at=now()""",
                        task.get("task_id") or task.get("id"),
                        run_data["run_id"], run_data.get("user_id", ""),
                        int(task.get("task_index", 0) or 0),
                        task.get("title") or "",
                        task.get("status", "PENDING"),
                        _json({k: v for k, v in task.items()
                               if k not in ("task_id", "id")}))

    async def get_run_full(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = await self.get_run(run_id)
        if run is None:
            return None
        rows = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE run_id=$1 ORDER BY task_index", run_id)
        tasks = []
        for r in rows:
            payload = _parse_json(r.get("payload")) or {}
            task = {**payload, "task_id": r["id"], "run_id": r["run_id"],
                    "status": r["status"], "title": r["title"],
                    "task_index": r["task_index"],
                    "lease_owner": r["lease_owner"], "lease_expires_at": r["lease_expires_at"],
                    "attempt": r["attempt"], "error": r["error"]}
            tasks.append(task)
        run["tasks"] = tasks
        run["run_id"] = run["id"]
        # The workflow identity rides the run row: workflow_id == the
        # workflow anchor; parent/thread ride the payload (sqlite parity).
        run.setdefault("workflow_id", run.get("workflow_id") or f"wf-{run['id'][:8]}")
        return run

    async def update_task(self, run_id: str, task_id: str,
                          updates: Dict[str, Any]) -> None:
        allowed = ("status", "attempt", "lease_expires_at", "error")
        sets, args, idx = [], [], 1
        payload_updates = {}
        for k, v in updates.items():
            if k in allowed:
                sets.append(f"{k}=${idx}")
                if k == "lease_expires_at" and isinstance(v, str):
                    v = datetime.fromisoformat(v)
                args.append(v)
                idx += 1
            else:
                payload_updates[k] = v
        if payload_updates:
            sets.append(f"payload = payload || ${idx}::jsonb")
            args.append(_json(payload_updates))
            idx += 1
        sets.append("updated_at=now()")
        args.extend([run_id, task_id])
        await self.db.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE run_id=${idx} AND id=${idx + 1}", *args)

    async def update_task_if_not_cancelled(self, run_id: str, task_id: str,
                                           updates: Dict[str, Any], *, expected_attempt=None) -> bool:
        async with self.db.pool.acquire() as conn, conn.transaction():
            # Cancellation updates the run first. Lock in the same order so
            # completion has a single ordering relative to cancellation.
            run = await conn.fetchrow("SELECT status FROM workflows WHERE id=$1 FOR UPDATE", run_id)
            if run is None or run["status"] == "CANCELLED":
                return False
            row = await conn.fetchrow("SELECT * FROM tasks WHERE run_id=$1 AND id=$2 FOR UPDATE", run_id, task_id)
            if row is None or row["status"] == "CANCELLED":
                return False
            if expected_attempt is not None:
                if (row["status"] != "RUNNING" or row["attempt"] != expected_attempt
                        or row["lease_expires_at"] is None
                        or row["lease_expires_at"] <= datetime.now(timezone.utc)):
                    return False
            columns = {key: value for key, value in updates.items()
                       if key in ("status", "attempt", "lease_expires_at", "error")}
            payload = {key: value for key, value in updates.items() if key not in columns}
            sets = ["updated_at=now()", "payload=payload || $3::jsonb"]
            args = [run_id, task_id, _json(payload)]
            for key, value in columns.items():
                if key == "lease_expires_at" and isinstance(value, str):
                    value = datetime.fromisoformat(value)
                args.append(value)
                sets.append(f"{key}=${len(args)}")
            await conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE run_id=$1 AND id=$2", *args)
            return True

    async def cancel_run(self, run_id: str, by: str = "",
                         reason: str = "") -> bool:
        row = await self.db.fetch_one(
            "UPDATE workflows SET status='CANCELLED', updated_at=now() "
            "WHERE id=$1 RETURNING id", run_id)
        await self.db.execute(
            "UPDATE tasks SET status='CANCELLED', updated_at=now() "
            "WHERE run_id=$1 AND status NOT IN ('COMPLETED','CANCELLED')",
            run_id)
        return row is not None

    async def update_task_deferral(self, run_id: str, task_id: str,
                                   next_attempt_at: str,
                                   defer_count: int) -> None:
        await self.db.execute(
            """UPDATE tasks SET payload = payload || $3::jsonb,
               updated_at=now() WHERE run_id=$1 AND id=$2""",
            run_id, task_id,
            _json({"next_attempt_at": next_attempt_at,
                   "defer_count": defer_count}))

    async def find_due_deferred_tasks(self, now_iso: str,
                                      limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT * FROM tasks
               WHERE status IN ('DEFERRED','RETRYING')
                 AND (payload->>'next_attempt_at')::timestamptz <= $1::timestamptz
               ORDER BY (payload->>'next_attempt_at')::timestamptz
               LIMIT $2""", now_iso, limit)
        out = []
        for r in rows:
            payload = _parse_json(r.get("payload")) or {}
            out.append({"run_id": r["run_id"], "task_id": r["id"],
                        "defer_count": int(payload.get("defer_count", 0)),
                        "next_attempt_at": payload.get("next_attempt_at")})
        return out

    async def save_approval(self, approval: Dict[str, Any]) -> None:
        from backend.repositories.postgres.repositories import (
            PostgresApprovalRepository, _json)
        repo = PostgresApprovalRepository(self.db)
        await repo.create_approval(approval)

    async def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        from backend.repositories.postgres.repositories import (
            PostgresApprovalRepository)
        repo = PostgresApprovalRepository(self.db)
        return await repo.get_approval(approval_id)

    async def save_event(self, event_data: Dict[str, Any]) -> None:
        """Durable workflow event (engine + SSE replay)."""
        if hasattr(event_data, "model_dump"):
            event_data = event_data.model_dump(mode="json")
        row = await self.db.fetch_one(
            """INSERT INTO events (run_id, user_id, event_type, payload)
               VALUES ($1,$2,$3,$4::jsonb) RETURNING id""",
            event_data.get("run_id", ""), event_data.get("user_id", ""),
            event_data.get("type") or event_data.get("event_type") or "EVENT",
            _json(event_data))
        return int(row["id"]) if row else 0

    async def list_thread_runs(self, user_id: str, thread_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM workflows WHERE user_id=$1 AND id::text IN "
            "(SELECT run_id FROM events WHERE payload->>'thread_id'=$1) "
            "ORDER BY created_at DESC LIMIT 50", user_id, thread_id)
        # The thread contract: runs sharing the thread anchor id.
        if not rows:
            row = await self.get_run(thread_id)
            if row and row["user_id"] == user_id:
                rows = [row]
        return rows

    async def purge_old_events(self, days: int) -> int:
        rows = await self.db.fetch_all(
            "DELETE FROM events WHERE created_at < now() - "
            "make_interval(days => $1) RETURNING id", days)
        return len(rows)
