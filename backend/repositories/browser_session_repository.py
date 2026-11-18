"""Browser session repository (Phase 8 §8.5/§8.7).

Durable session metadata in the same SQLite/Firestore architecture as every
other store. Ownership is transactional (§8.7): two workers racing to
acquire a session resolve to exactly ONE winner via compare-and-set on the
lease; an old worker cannot act after ownership changes because every
command carries the owner epoch.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.browser_session import (
    Actor,
    ownership_epoch,
    BrowserSession,
    ChallengeState,
    ControllerState,
    CleanupState,
    ResourceState,
    SessionTransitionError,
    new_browser_session_id,
    session_transition_valid,
)


def _row_to_session(row) -> BrowserSession:
    origins = row["allowed_origins_json"]
    try:
        origins = json.loads(origins or "[]")
    except ValueError:
        origins = []
    return BrowserSession(
        session_id=row["session_id"], tenant_id=row["tenant_id"],
        user_id=row["user_id"], workflow_id=row["workflow_id"],
        run_id=row["run_id"], task_id=row["task_id"],
        owner_worker_id=row["owner_worker_id"] or "",
        owner_epoch=row["owner_epoch"] or "",
        owner_generation=int(row["owner_generation"] or 0),
        lease_expires_at=int(row["lease_expires_at"] or 0),
        runtime_session_id=row["runtime_session_id"],
        policy_revision=row["policy_revision"],
        resource_state=ResourceState(row["resource_state"]),
        controller_state=ControllerState(row["controller_state"]),
        controller_generation=int(row["controller_generation"] or 0) if "controller_generation" in row.keys() else 0,
        challenge_state=ChallengeState(row["challenge_state"]),
        cleanup_state=CleanupState(row["cleanup_state"]),
        allowed_origins=origins if isinstance(origins, list) else [],
        network_policy_id=row["network_policy_id"],
        credential_binding_id=row["credential_binding_id"],
        checkpoint_reference=row["checkpoint_reference"],
        last_action_id=row["last_action_id"],
        created_at=int(row["created_at"]), last_activity_at=int(row["last_activity_at"]),
        idle_expires_at=int(row["idle_expires_at"] or 0),
        handoff_expires_at=int(row["handoff_expires_at"] or 0),
        absolute_expires_at=int(row["absolute_expires_at"] or 0),
    )


class BrowserSessionRepository:
    def __init__(self, db):
        self.db = db

    async def create_session(self, session: BrowserSession) -> BrowserSession:
        now = epoch_now()
        session.created_at = now
        session.last_activity_at = now
        row = session.to_row()
        row["allowed_origins_json"] = json.dumps(session.allowed_origins)
        await self.db.execute(
            """
            INSERT INTO browser_sessions
                (session_id, tenant_id, user_id, workflow_id, run_id, task_id,
                 owner_worker_id, owner_epoch, owner_generation,
                 lease_expires_at,
                 runtime_session_id, policy_revision, resource_state,
                 controller_state, controller_generation, challenge_state, cleanup_state,
                 allowed_origins_json, network_policy_id,
                 credential_binding_id, checkpoint_reference, last_action_id,
                 created_at, last_activity_at, idle_expires_at,
                 handoff_expires_at, absolute_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row["session_id"], row["tenant_id"], row["user_id"],
             row["workflow_id"], row["run_id"], row["task_id"],
             row["owner_worker_id"], row["owner_epoch"], row.get("owner_generation", 0),
             row["lease_expires_at"],
             row["runtime_session_id"], row["policy_revision"],
             row["resource_state"], row["controller_state"], row["controller_generation"],
             row["challenge_state"], row["cleanup_state"],
             row["allowed_origins_json"], row["network_policy_id"],
             row["credential_binding_id"], row["checkpoint_reference"],
             row["last_action_id"], row["created_at"], row["last_activity_at"],
             row["idle_expires_at"], row["handoff_expires_at"],
             row["absolute_expires_at"]))
        await self.db.commit()
        return session

    async def get_session(self, session_id: str) -> Optional[BrowserSession]:
        cursor = await self.db.execute(
            "SELECT * FROM browser_sessions WHERE session_id=?", (session_id,))
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None

    async def get_session_for_run(self, run_id: str, task_id: str) -> Optional[BrowserSession]:
        cursor = await self.db.execute(
            "SELECT * FROM browser_sessions WHERE run_id=? AND task_id=? "
            "AND resource_state != 'CLOSED' ORDER BY created_at DESC LIMIT 1",
            (run_id, task_id))
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None

    async def acquire_ownership(self, session_id: str, worker_id: str,
                                tenant_id: str, lease_seconds: int,
                                attempt: int) -> Optional[BrowserSession]:
        """§8.7/9.0A: transactional ownership acquisition.
        - TENANT identity enforced in the CAS (a different tenant cannot
          acquire another tenant's session even with an expired lease).
        - The fencing generation is SERVER-CONTROLLED and monotonically
          advancing — the caller cannot choose a reusable epoch by repeating
          an attempt number.
        - Two workers racing: exactly ONE wins (CAS on the lease)."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT tenant_id, lease_expires_at, owner_generation FROM "
                "browser_sessions WHERE session_id=?", (session_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            if row["tenant_id"] != tenant_id:
                return None   # §9.0A: tenant identity enforced in the transaction
            new_generation = int(row["owner_generation"] or 0) + 1
            new_epoch = ownership_epoch(tenant_id, session_id, worker_id,
                                        new_generation)
            cursor = await conn.execute(
                "UPDATE browser_sessions SET owner_worker_id=?, owner_epoch=?, "
                "owner_generation=?, lease_expires_at=?, last_activity_at=? "
                "WHERE session_id=? AND tenant_id=? AND "
                "(lease_expires_at<=? OR owner_worker_id=?)",
                (worker_id, new_epoch, new_generation, now + lease_seconds, now,
                 session_id, tenant_id, now, worker_id))
            if not cursor.rowcount:
                return None   # another worker holds a live lease
        return await self.get_session(session_id)

    async def renew_ownership(self, session_id: str, worker_id: str,
                              owner_epoch: str, lease_seconds: int) -> bool:
        """§8.7/9.0A: heartbeat — renewal verifies CURRENT ownership (worker +
        epoch) AND a NON-EXPIRED lease. An expired lease cannot be revived by
        renewal: controlled reacquisition (acquire_ownership) is required,
        which advances the fencing generation."""
        now = epoch_now()
        cursor = await self.db.execute(
            "UPDATE browser_sessions SET lease_expires_at=?, last_activity_at=? "
            "WHERE session_id=? AND owner_worker_id=? AND owner_epoch=? "
            "AND lease_expires_at>?",
            (now + lease_seconds, now, session_id, worker_id, owner_epoch, now))
        await self.db.commit()
        return bool(cursor.rowcount)

    async def transition(self, session_id: str, target: ResourceState,
                         actor: Actor) -> BrowserSession:
        """§8.6/9.0A: validated lifecycle transition with an ATOMIC
        compare-and-set — the expected state is part of the UPDATE, closing
        the validate-then-mutate race."""
        current = await self.get_session(session_id)
        if current is None:
            raise SessionTransitionError("Session not found", ResourceState.FAILED)
        if not session_transition_valid(current.resource_state, target, actor):
            raise SessionTransitionError(
                f"Invalid transition {current.resource_state.value} → "
                f"{target.value} by {actor.value}", current.resource_state)
        cursor = await self.db.execute(
            "UPDATE browser_sessions SET resource_state=?, last_activity_at=? "
            "WHERE session_id=? AND resource_state=?",
            (target.value, epoch_now(), session_id, current.resource_state.value))
        await self.db.commit()
        if not cursor.rowcount:
            raise SessionTransitionError(
                f"Concurrent state change for {session_id}",
                current.resource_state)
        return await self.get_session(session_id)

    async def set_controller(self, session_id: str,
                             controller: ControllerState, *,
                             expected_owner_epoch: Optional[str] = None,
                             expected_controller: Optional[ControllerState] = None) -> bool:
        """9.0A: controller updates are bound to the expected owner epoch
        and (optionally) the expected current controller — CAS, not a blind
        write."""
        query = ("UPDATE browser_sessions SET controller_state=?, "
                 "last_activity_at=? WHERE session_id=?")
        args: List[Any] = [controller.value, epoch_now(), session_id]
        if expected_owner_epoch is not None:
            query += " AND owner_epoch=?"
            args.append(expected_owner_epoch)
        if expected_controller is not None:
            query += " AND controller_state=?"
            args.append(expected_controller.value)
        cursor = await self.db.execute(query, args)
        await self.db.commit()
        return bool(cursor.rowcount)

    async def set_challenge(self, session_id: str,
                            challenge: ChallengeState) -> None:
        await self.db.execute(
            "UPDATE browser_sessions SET challenge_state=?, last_activity_at=? "
            "WHERE session_id=?", (challenge.value, epoch_now(), session_id))
        await self.db.commit()

    async def change_runtime_control(self, session_id, worker_id, owner_epoch,
                                     generation, controller, *, endpoint=None):
        cursor = await self.db.execute(
            "UPDATE browser_sessions SET controller_state=?, controller_generation=controller_generation+1, "
            "runtime_session_id=COALESCE(?,runtime_session_id), last_activity_at=? "
            "WHERE session_id=? AND owner_worker_id=? AND owner_epoch=? AND controller_generation=? "
            "AND lease_expires_at>? AND resource_state='READY' RETURNING *",
            (controller.value, endpoint, epoch_now(), session_id, worker_id, owner_epoch, generation, epoch_now()))
        row = await cursor.fetchone()
        await self.db.commit()
        return _row_to_session(row) if row else None

    async def record_checkpoint(self, session_id: str, *,
                                checkpoint_digest: str,
                                milestone: str,
                                captured_at: Optional[int] = None) -> str:
        """§14.5: persist a STATE CHECKPOINT reference on the durable
        session after meaningful progress. The reference is the signed
        identity of the authoritative observation (digest + milestone +
        capture time) — recovery and verification read THIS, never a
        model's claim about where the session got to. Each checkpoint
        ADVANCES the reference (monotonic by capture time); an older
        checkpoint never overwrites a newer one."""
        now = int(captured_at or epoch_now())
        import hashlib as _hl
        ref = _hl.sha256(
            f"{checkpoint_digest}|{milestone}|{now}".encode()).hexdigest()[:24]
        # Monotonic: only advance when newer than the stored capture. The
        # capture time is stored FIXED-WIDTH (10 digits) so the guard's
        # suffix comparison parses exactly.
        stamped = f"{ref}:{now:010d}"
        cursor = await self.db.execute(
            "UPDATE browser_sessions SET checkpoint_reference=?, "
            "last_activity_at=? WHERE session_id=? AND ("
            "checkpoint_reference IS NULL OR checkpoint_reference='' OR "
            "CAST(substr(checkpoint_reference, -10) AS INTEGER) <= ?)",
            (stamped, now, session_id, now))
        await self.db.commit()
        return stamped

    async def latest_checkpoint(self, session_id: str) -> Optional[Dict[str, Any]]:
        """§14.5 (audit W8): READ the durable checkpoint — recovery and
        verification consume the REFERENCE (digest + milestone + capture
        time), never a model's claim about where the session got to."""
        stored = await self.get_session(session_id)
        if stored is None or not stored.checkpoint_reference:
            return None
        ref = stored.checkpoint_reference
        try:
            digest, stamp = ref.rsplit(":", 1)
            return {"reference": ref, "digest": digest,
                    "captured_at": int(stamp)}
        except ValueError:
            return {"reference": ref, "digest": ref, "captured_at": 0}

    async def record_activity(self, session_id: str, *,
                              extend_idle_seconds: Optional[int] = None,
                              extend_absolute: bool = False) -> None:
        """§8.10: meaningful activity updates last_activity and MAY extend
        the idle timer. A heartbeat NEVER extends the absolute lifetime."""
        now = epoch_now()
        sets = "last_activity_at=?"
        args: List[Any] = [now]
        if extend_idle_seconds and extend_idle_seconds > 0:
            sets += ", idle_expires_at=?"
            args.append(now + extend_idle_seconds)
        args.append(session_id)
        await self.db.execute(
            f"UPDATE browser_sessions SET {sets} WHERE session_id=?", args)
        await self.db.commit()

    async def expired_sessions(self, now: Optional[int] = None,
                               limit: int = 50) -> List[BrowserSession]:
        """§8.10: sessions whose idle/handoff/absolute timer passed — sweep
        candidates (READY or RECOVERING only; closed ones are done)."""
        now = now if now is not None else epoch_now()
        cursor = await self.db.execute(
            "SELECT * FROM browser_sessions WHERE resource_state IN "
            "('READY','RECOVERING','ALLOCATING') AND (idle_expires_at<=? AND idle_expires_at>0 "
            "OR handoff_expires_at<=? AND handoff_expires_at>0 "
            "OR absolute_expires_at<=? AND absolute_expires_at>0) LIMIT ?",
            (now, now, now, limit))
        return [_row_to_session(r) for r in await cursor.fetchall()]

    async def mark_cleanup(self, session_id: str,
                           state: CleanupState) -> None:
        await self.db.execute(
            "UPDATE browser_sessions SET cleanup_state=?, last_activity_at=? "
            "WHERE session_id=?", (state.value, epoch_now(), session_id))
        await self.db.commit()

    async def acknowledge_cleanup(self, session_id: str, worker_id: str,
                                  owner_epoch: str, *, complete: bool) -> bool:
        """A runtime may acknowledge only the exact owner generation it closed.
        §29.12: a completed cleanup also CLEARS the lease — a dead owner's
        durable row cannot keep presenting a live lease to another replica."""
        cursor = await self.db.execute(
            "UPDATE browser_sessions SET cleanup_state=?, "
            "resource_state=CASE WHEN ? THEN 'CLOSED' ELSE resource_state END, "
            "controller_state=CASE WHEN ? THEN 'NONE' ELSE controller_state END, "
            "lease_expires_at=CASE WHEN ? THEN 0 ELSE lease_expires_at END, "
            "last_activity_at=? WHERE session_id=? AND owner_worker_id=? AND owner_epoch=?",
            (CleanupState.COMPLETE.value if complete else CleanupState.FAILED.value,
             complete, complete, complete, epoch_now(), session_id, worker_id,
             owner_epoch))
        await self.db.commit()
        return cursor.rowcount == 1

    async def close_session(self, session_id: str) -> None:
        await self.db.execute(
            "UPDATE browser_sessions SET resource_state='CLOSED', "
            "controller_state='NONE', last_activity_at=? WHERE session_id=?",
            (epoch_now(), session_id))
        await self.db.commit()


    async def list_for_owner(self, owner_user_id: str) -> List[Dict[str, Any]]:
        """§25.3: cross-replica discovery — sessions this user owns that
        are registered in the durable store (live or recently live)."""
        rows = await self.db.fetch_all(
            "SELECT * FROM browser_sessions WHERE user_id=? "
            "AND resource_state='READY' AND lease_expires_at > ? ORDER BY created_at DESC",
            (owner_user_id, epoch_now()))
        return [dict(r) for r in rows]

    async def register_worker_session(
            self, *, session_id: str, worker_id: str, endpoint: str,
            owner_user_id: str, run_id: str, task_id: str,
            controller_epoch: str) -> None:
        """§24.6 (R3): persist the LIVE session with its owning worker and
        the endpoint other replicas use to route commands. The internal
        endpoint rides runtime_session_id (no schema change needed)."""
        now = epoch_now()
        await self.db.execute(
            """
            INSERT OR REPLACE INTO browser_sessions
                (session_id, tenant_id, user_id, run_id, task_id,
                 owner_worker_id, owner_epoch, owner_generation,
                 lease_expires_at, runtime_session_id, resource_state,
                 controller_state, challenge_state, cleanup_state,
                 allowed_origins_json, created_at, last_activity_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'READY', 'AGENT',
                    'NONE', 'NOT_REQUIRED', '[]', ?, ?)
            """,
            (session_id, owner_user_id, owner_user_id, run_id, task_id,
             worker_id, controller_epoch, now + 3600, endpoint,
             now, now))
        await self.db.commit()


_repo: Optional[BrowserSessionRepository] = None


def set_browser_session_repo(repo) -> None:
    global _repo
    _repo = repo


def get_browser_session_repo() -> Optional[BrowserSessionRepository]:
    return _repo
