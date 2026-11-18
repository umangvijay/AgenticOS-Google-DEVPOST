"""Typed browser lifecycle on PostgreSQL, serialized with row-level locks.

The legacy viewer registry remains readable. Typed sessions store the same
versioned dimensions as SQLite; routing projections update in the same transaction.
"""
import hashlib
import json
from datetime import datetime, timezone

from backend.models.admission import epoch_now
from backend.models.browser_session import (
    Actor, BrowserSession, CleanupState, ControllerState, ResourceState, SessionTransitionError,
    ownership_epoch, session_transition_valid,
)
from backend.repositories.browser_session_repository import _row_to_session


def decode(value):
    data = json.loads(value) if isinstance(value, str) else dict(value)
    # The shared SQLite decoder expects the serialized origins column.
    if isinstance(data.get("allowed_origins_json"), list):
        data["allowed_origins_json"] = json.dumps(data["allowed_origins_json"])
    return data


class PostgresBrowserContract:
    async def create_session(self, session):
        if not isinstance(session, BrowserSession):
            return await super().create_session(session)
        session.created_at = session.last_activity_at = epoch_now()
        row = session.to_row()
        await self.db.execute(
            """INSERT INTO pg_browser_sessions
               (session_id,user_id,run_id,task_id,state,owner_worker_id,runtime_session_id,
                controller_worker_id,controller_epoch,lease_expires_at,allowed_origins,session_metadata)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$6,$8,$9,$10::jsonb,$11::jsonb)""",
            session.session_id, session.user_id, session.run_id or "", session.task_id or "",
            session.resource_state.value, session.owner_worker_id, session.runtime_session_id,
            session.owner_epoch, datetime.fromtimestamp(session.lease_expires_at, timezone.utc),
            json.dumps(session.allowed_origins), json.dumps(row))
        return session

    async def get_session(self, session_id):
        row = await self.db.fetch_one("SELECT * FROM pg_browser_sessions WHERE session_id=$1", session_id)
        if not row:
            return None
        if row.get("session_metadata") is not None:
            return _row_to_session(decode(row["session_metadata"]))
        return self._shape(row)

    async def get_session_for_run(self, run_id, task_id):
        row = await self.db.fetch_one(
            """SELECT session_id FROM pg_browser_sessions WHERE run_id=$1 AND task_id=$2
               AND state <> 'CLOSED' ORDER BY created_at DESC LIMIT 1""", run_id, task_id)
        return await self.get_session(row["session_id"]) if row else None

    async def _edit_browser(self, session_id, mutate):
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT session_metadata FROM pg_browser_sessions WHERE session_id=$1 FOR UPDATE", session_id)
            if row is None or row["session_metadata"] is None:
                raise SessionTransitionError("Typed browser session not found", ResourceState.FAILED)
            session = _row_to_session(decode(row["session_metadata"]))
            result = mutate(session)
            if result is False:
                return False
            session.last_activity_at = epoch_now()
            await conn.execute(
                """UPDATE pg_browser_sessions SET session_metadata=$2::jsonb,state=$3,
                   owner_worker_id=$4,controller_worker_id=$4,controller_epoch=$5,
                   lease_expires_at=$6,runtime_session_id=$7,last_activity_at=now()
                   WHERE session_id=$1""", session_id, json.dumps(session.to_row()),
                session.resource_state.value, session.owner_worker_id, session.owner_epoch,
                datetime.fromtimestamp(session.lease_expires_at, timezone.utc), session.runtime_session_id)
            return session if result is None else result

    async def acquire_ownership(self, session_id, worker_id, tenant_id, lease_seconds, attempt=None):
        if attempt is None:
            # Explicit compatibility with previously persisted viewer rows.
            if isinstance(await self.get_session(session_id), BrowserSession):
                raise TypeError("typed ownership requires tenant and attempt")
            return await super().acquire_ownership(session_id, worker_id, tenant_id, lease_seconds)
        def acquire(session):
            now = epoch_now()
            if (session.tenant_id != tenant_id or session.resource_state in
                    (ResourceState.CLOSED, ResourceState.CLOSING, ResourceState.FAILED)
                    or session.lease_expires_at > now and session.owner_worker_id != worker_id):
                return False
            session.owner_generation += 1
            session.owner_worker_id = worker_id
            session.owner_epoch = ownership_epoch(tenant_id, session_id, worker_id, session.owner_generation)
            session.lease_expires_at = now + lease_seconds
        result = await self._edit_browser(session_id, acquire)
        return result or None

    async def renew_ownership(self, session_id, worker_id, owner_epoch, lease_seconds=None):
        if lease_seconds is None:
            if isinstance(await self.get_session(session_id), BrowserSession):
                raise TypeError("typed renewal requires owner epoch")
            return await super().renew_ownership(session_id, worker_id, owner_epoch)
        def renew(session):
            now = epoch_now()
            if (session.owner_worker_id != worker_id or session.owner_epoch != owner_epoch
                    or session.lease_expires_at <= now or session.resource_state is ResourceState.CLOSED):
                return False
            session.lease_expires_at = now + lease_seconds
            return True
        return await self._edit_browser(session_id, renew)

    async def transition(self, session_id, target, actor=None):
        if actor is None:
            if isinstance(await self.get_session(session_id), BrowserSession):
                raise TypeError("typed transitions require an actor")
            return await super().transition(session_id, target)
        def transition(session):
            if not session_transition_valid(session.resource_state, target, actor):
                raise SessionTransitionError("Invalid browser lifecycle transition", session.resource_state)
            session.resource_state = target
        return await self._edit_browser(session_id, transition)

    async def set_controller(self, session_id, controller, *legacy,
                             expected_owner_epoch=None, expected_controller=None):
        if legacy:
            if isinstance(await self.get_session(session_id), BrowserSession):
                raise TypeError("typed controller updates require compare-and-set arguments")
            return await super().set_controller(session_id, controller, *legacy)
        def change(session):
            if ((expected_owner_epoch is not None and session.owner_epoch != expected_owner_epoch)
                    or (expected_controller is not None and session.controller_state != expected_controller)):
                return False
            session.controller_state = ControllerState(controller)
            return True
        return await self._edit_browser(session_id, change)

    async def set_challenge(self, session_id, challenge):
        if not isinstance(await self.get_session(session_id), BrowserSession):
            return await super().set_challenge(session_id, challenge)
        await self._edit_browser(session_id, lambda session: setattr(session, "challenge_state", challenge))

    async def record_checkpoint(self, session_id, *legacy, checkpoint_digest=None,
                                milestone=None, captured_at=None):
        if legacy:
            if isinstance(await self.get_session(session_id), BrowserSession):
                raise TypeError("typed checkpoints require named digest and milestone")
            return await super().record_checkpoint(session_id, *legacy)
        if not checkpoint_digest or not milestone:
            raise ValueError("checkpoint digest and milestone required")
        now = int(captured_at or epoch_now())
        digest = hashlib.sha256(f"{checkpoint_digest}|{milestone}|{now}".encode()).hexdigest()[:24]
        stamped = f"{digest}:{now:010d}"
        def checkpoint(session):
            previous = session.checkpoint_reference
            if previous and int(previous.rsplit(":", 1)[1]) > now:
                return previous
            session.checkpoint_reference = stamped
            return stamped
        return await self._edit_browser(session_id, checkpoint)

    async def latest_checkpoint(self, session_id):
        session = await self.get_session(session_id)
        if not isinstance(session, BrowserSession):
            return await super().latest_checkpoint(session_id)
        if not session.checkpoint_reference:
            return None
        digest, stamp = session.checkpoint_reference.rsplit(":", 1)
        return {"reference": session.checkpoint_reference, "digest": digest, "captured_at": int(stamp)}

    async def record_activity(self, session_id, *, extend_idle_seconds=None, extend_absolute=False):
        if not isinstance(await self.get_session(session_id), BrowserSession):
            return await super().record_activity(session_id)
        def activity(session):
            if extend_idle_seconds and extend_idle_seconds > 0:
                session.idle_expires_at = epoch_now() + extend_idle_seconds
        await self._edit_browser(session_id, activity)

    async def expired_sessions(self, now=None, limit=50):
        now = epoch_now() if now is None else now
        rows = await self.db.fetch_all(
            """SELECT session_metadata FROM pg_browser_sessions
               WHERE session_metadata IS NOT NULL AND state IN ('ALLOCATING','READY','RECOVERING')
               AND EXISTS (SELECT 1 FROM unnest(ARRAY['idle_expires_at','handoff_expires_at',
                   'absolute_expires_at']) AS timer(name)
                   WHERE (session_metadata->>timer.name)::double precision > 0
                   AND (session_metadata->>timer.name)::double precision <= $1)
               ORDER BY created_at LIMIT $2""", now, limit)
        return [_row_to_session(decode(row["session_metadata"])) for row in rows]

    async def mark_cleanup(self, session_id, state):
        await self._edit_browser(session_id, lambda session: setattr(session, "cleanup_state", state))

    async def change_runtime_control(self, session_id, worker_id, owner_epoch,
                                     generation, controller, *, endpoint=None):
        def change(session):
            if (session.owner_worker_id != worker_id or session.owner_epoch != owner_epoch
                    or session.controller_generation != generation
                    or session.lease_expires_at <= epoch_now() or session.resource_state is not ResourceState.READY):
                return False
            session.controller_state = controller
            session.controller_generation += 1
            if endpoint is not None:
                session.runtime_session_id = endpoint
        return await self._edit_browser(session_id, change) or None

    async def acknowledge_cleanup(self, session_id, worker_id, owner_epoch, *, complete):
        def acknowledge(session):
            if session.owner_worker_id != worker_id or session.owner_epoch != owner_epoch:
                return False
            session.cleanup_state = CleanupState.COMPLETE if complete else CleanupState.FAILED
            if complete:
                session.resource_state = ResourceState.CLOSED
                session.controller_state = ControllerState.NONE
                session.lease_expires_at = 0  # §29.12: a closed session is not live
            return True
        return await self._edit_browser(session_id, acknowledge)

    async def close_session(self, session_id):
        if not isinstance(await self.get_session(session_id), BrowserSession):
            return await super().close_session(session_id)
        def close(session):
            session.resource_state = ResourceState.CLOSED
            session.controller_state = ControllerState.NONE
        await self._edit_browser(session_id, close)
