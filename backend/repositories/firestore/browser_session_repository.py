"""Firestore browser-session repository (Phase 8/9 — parity).

The SAME contract as the SQLite BrowserSessionRepository against
Firestore:

- Deterministic document identity: browser_sessions/{session_id}.
- §8.7/§9.0A ownership acquisition is a CAS inside an async transaction:
  TENANT identity, lease expiry, and the SERVER-CONTROLLED monotonically
  advancing generation all resolve atomically — two workers racing
  produce exactly ONE winner, and a foreign tenant can never acquire
  another tenant's session.
- Lifecycle transitions enforce the SHARED state-machine tables imported
  from the SQLite module with expected-state CAS.
- Sweeps sort client-side: no composite-index prerequisite.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now
from backend.models.browser_session import (
    Actor,
    BrowserSession,
    ChallengeState,
    ControllerState,
    CleanupState,
    ResourceState,
    SessionTransitionError,
    new_browser_session_id,
    ownership_epoch,
    session_transition_valid,
)

logger = logging.getLogger(__name__)

COLLECTION = "browser_sessions"


def _doc_to_session(data: Dict[str, Any], session_id: str) -> BrowserSession:
    return BrowserSession(
        session_id=session_id,
        tenant_id=data.get("tenant_id", ""),
        user_id=data.get("user_id", ""),
        workflow_id=data.get("workflow_id"),
        run_id=data.get("run_id"),
        task_id=data.get("task_id"),
        owner_worker_id=data.get("owner_worker_id") or "",
        owner_epoch=data.get("owner_epoch") or "",
        owner_generation=int(data.get("owner_generation") or 0),
        lease_expires_at=int(data.get("lease_expires_at") or 0),
        runtime_session_id=data.get("runtime_session_id"),
        policy_revision=data.get("policy_revision"),
        resource_state=ResourceState(data.get("resource_state", "ALLOCATING")),
        controller_state=ControllerState(data.get("controller_state", "NONE")),
        controller_generation=int(data.get("controller_generation") or 0),
        challenge_state=ChallengeState(data.get("challenge_state", "NONE")),
        cleanup_state=CleanupState(data.get("cleanup_state", "NOT_REQUIRED")),
        allowed_origins=list(data.get("allowed_origins") or []),
        network_policy_id=data.get("network_policy_id"),
        credential_binding_id=data.get("credential_binding_id"),
        checkpoint_reference=data.get("checkpoint_reference"),
        last_action_id=data.get("last_action_id"),
        created_at=int(data.get("created_at") or 0),
        last_activity_at=int(data.get("last_activity_at") or 0),
        idle_expires_at=int(data.get("idle_expires_at") or 0),
        handoff_expires_at=int(data.get("handoff_expires_at") or 0),
        absolute_expires_at=int(data.get("absolute_expires_at") or 0),
    )


def _session_to_doc(session: BrowserSession) -> Dict[str, Any]:
    row = session.to_row()
    row.pop("allowed_origins_json", None)
    doc = dict(row)
    doc.pop("session_id", None)
    doc["allowed_origins"] = list(session.allowed_origins)
    return doc


class FirestoreBrowserSessionRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_session(self, session: BrowserSession) -> BrowserSession:
        now = epoch_now()
        session.created_at = now
        session.last_activity_at = now
        db = await self._get_db()
        await db.collection(COLLECTION).document(session.session_id).create(
            _session_to_doc(session))
        return session

    async def get_session(self, session_id: str) -> Optional[BrowserSession]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            session_id).get()
        if not snap.exists:
            return None
        return _doc_to_session(snap.to_dict() or {}, session_id)

    async def get_session_for_run(self, run_id: str, task_id: str) -> Optional[BrowserSession]:
        db = await self._get_db()
        query = (db.collection(COLLECTION).where("run_id", "==", run_id)
                 .where("task_id", "==", task_id).limit(10))
        best: Optional[BrowserSession] = None
        async for snap in query.stream():
            s = _doc_to_session(snap.to_dict() or {}, snap.id)
            if s.resource_state is ResourceState.CLOSED:
                continue
            if best is None or s.created_at > best.created_at:
                best = s
        return best

    async def list_for_owner(self, owner_user_id: str):
        db = await self._get_db()
        sessions = []
        async for doc in db.collection(COLLECTION).where("user_id", "==", owner_user_id).stream():
            session = _doc_to_session(doc.to_dict() or {}, doc.id)
            if session.resource_state is ResourceState.READY and session.lease_expires_at > epoch_now():
                sessions.append(session)
        return sorted(sessions, key=lambda session: session.created_at, reverse=True)

    async def acquire_ownership(self, session_id: str, worker_id: str,
                                tenant_id: str, lease_seconds: int,
                                attempt: int) -> Optional[BrowserSession]:
        """§8.7/§9.0A on Firestore: transactional ownership acquisition.
        TENANT identity enforced in the transaction; the fencing generation
        is server-controlled and monotonically advancing; two workers
        racing → exactly ONE winner."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(session_id)

        @fs_types.async_transactional
        async def acquire(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get("tenant_id") != tenant_id:
                return None   # §9.0A: tenant identity enforced in the transaction
            lease_expires_at = int(data.get("lease_expires_at") or 0)
            if lease_expires_at > now and data.get("owner_worker_id") != worker_id:
                return None   # another worker holds a live lease
            new_generation = int(data.get("owner_generation") or 0) + 1
            new_epoch = ownership_epoch(tenant_id, session_id, worker_id,
                                        new_generation)
            transaction.update(ref, {
                "owner_worker_id": worker_id,
                "owner_epoch": new_epoch,
                "owner_generation": new_generation,
                "lease_expires_at": now + lease_seconds,
                "last_activity_at": now,
            })
            return True

        if await acquire(db.transaction()) is not True:
            return None
        return await self.get_session(session_id)

    async def renew_ownership(self, session_id: str, worker_id: str,
                              owner_epoch: str, lease_seconds: int) -> bool:
        """§8.7/§9.0A on Firestore: renewal verifies CURRENT ownership
        (worker + epoch) AND a NON-EXPIRED lease — an expired lease cannot
        be revived by renewal; controlled reacquisition is required."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(session_id)

        @fs_types.async_transactional
        async def renew(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("owner_worker_id") != worker_id \
                    or data.get("owner_epoch") != owner_epoch \
                    or int(data.get("lease_expires_at") or 0) <= now:
                return False
            transaction.update(ref, {
                "lease_expires_at": now + lease_seconds,
                "last_activity_at": now,
            })
            return True

        return await renew(db.transaction()) is True

    async def transition(self, session_id: str, target: ResourceState,
                         actor: Actor) -> BrowserSession:
        """§8.6/§9.0A on Firestore: validated lifecycle transition with the
        expected state inside the transaction — the validate-then-mutate
        race closes on transaction commit, not on rowcount."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(session_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise SessionTransitionError("Session not found",
                                             ResourceState.FAILED)
            current_state = ResourceState(
                (snap.to_dict() or {}).get("resource_state", "ALLOCATING"))
            if not session_transition_valid(current_state, target, actor):
                raise SessionTransitionError(
                    f"Invalid transition {current_state.value} → "
                    f"{target.value} by {actor.value}", current_state)
            transaction.update(ref, {
                "resource_state": target.value, "last_activity_at": now,
            })

        await apply(db.transaction())
        return await self.get_session(session_id)

    async def set_controller(self, session_id: str,
                             controller: ControllerState, *,
                             expected_owner_epoch: Optional[str] = None,
                             expected_controller: Optional[ControllerState] = None) -> bool:
        """§9.0A on Firestore: controller updates bound to the expected
        owner epoch and (optionally) expected current controller — CAS in
        a transaction, not a blind write."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(session_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if expected_owner_epoch is not None \
                    and data.get("owner_epoch") != expected_owner_epoch:
                return False
            if expected_controller is not None \
                    and data.get("controller_state") != expected_controller.value:
                return False
            transaction.update(ref, {
                "controller_state": controller.value, "last_activity_at": now,
            })
            return True

        return await apply(db.transaction()) is True

    async def set_challenge(self, session_id: str,
                            challenge: ChallengeState) -> None:
        await (await self._get_db()).collection(COLLECTION).document(
            session_id).update({
                "challenge_state": challenge.value,
                "last_activity_at": epoch_now(),
            })

    async def record_activity(self, session_id: str, *,
                              extend_idle_seconds: Optional[int] = None,
                              extend_absolute: bool = False) -> None:
        """§8.10: meaningful activity updates last_activity and MAY extend
        the idle timer. A heartbeat NEVER extends the absolute lifetime."""
        now = epoch_now()
        update: Dict[str, Any] = {"last_activity_at": now}
        if extend_idle_seconds and extend_idle_seconds > 0:
            update["idle_expires_at"] = now + extend_idle_seconds
        await (await self._get_db()).collection(COLLECTION).document(
            session_id).update(update)

    async def expired_sessions(self, now: Optional[int] = None,
                               limit: int = 50) -> List[BrowserSession]:
        """§8.10 on Firestore: sessions whose idle/handoff/absolute timer
        passed (READY/RECOVERING/ALLOCATING only)."""
        db = await self._get_db()
        now = now if now is not None else epoch_now()
        out: List[BrowserSession] = []
        for state in ("READY", "RECOVERING", "ALLOCATING"):
            query = (db.collection(COLLECTION).where("resource_state", "==", state)
                     .limit(limit))
            async for snap in query.stream():
                s = _doc_to_session(snap.to_dict() or {}, snap.id)
                idle_hit = s.idle_expires_at and s.idle_expires_at <= now
                handoff_hit = s.handoff_expires_at and s.handoff_expires_at <= now
                absolute_hit = s.absolute_expires_at and s.absolute_expires_at <= now
                if idle_hit or handoff_hit or absolute_hit:
                    out.append(s)
        out.sort(key=lambda s: s.created_at)
        return out[:limit]

    async def mark_cleanup(self, session_id: str,
                           state: CleanupState) -> None:
        await (await self._get_db()).collection(COLLECTION).document(
            session_id).update({
                "cleanup_state": state.value, "last_activity_at": epoch_now(),
            })

    async def acknowledge_cleanup(self, session_id: str, worker_id: str,
                                  owner_epoch: str, *, complete: bool) -> bool:
        db = await self._get_db()
        ref = db.collection(COLLECTION).document(session_id)

        @fs_types.async_transactional
        async def acknowledge(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            data = snap.to_dict() or {}
            if data.get("owner_worker_id") != worker_id or data.get("owner_epoch") != owner_epoch:
                return False
            update = {"cleanup_state": "COMPLETE" if complete else "FAILED",
                      "last_activity_at": epoch_now()}
            if complete:
                update.update(resource_state="CLOSED", controller_state="NONE",
                              lease_expires_at=0)  # §29.12: closed is not live
            transaction.update(ref, update)
            return True

        return await acknowledge(db.transaction()) is True

    async def close_session(self, session_id: str) -> None:
        await (await self._get_db()).collection(COLLECTION).document(
            session_id).update({
                "resource_state": "CLOSED", "controller_state": "NONE",
                "last_activity_at": epoch_now(),
            })

    async def change_runtime_control(self, session_id, worker_id, owner_epoch,
                                     generation, controller, *, endpoint=None):
        import asyncio
        import random
        from google.api_core.exceptions import Aborted, FailedPrecondition, NotFound
        db = await self._get_db()
        ref = db.collection(COLLECTION).document(session_id)
        snap = await ref.get()
        data = snap.to_dict() or {}
        if (not snap.exists or data.get("owner_worker_id") != worker_id
                or data.get("owner_epoch") != owner_epoch
                or int(data.get("controller_generation") or 0) != generation
                or data.get("lease_expires_at", 0) <= epoch_now()
                or data.get("resource_state") != "READY"):
            return None
        update = {"controller_state": controller.value, "controller_generation": generation + 1,
                  "last_activity_at": epoch_now()}
        if endpoint is not None:
            update["runtime_session_id"] = endpoint
        for attempt in range(5):
            try:
                # Retrying this SAME document version cannot overwrite a
                # competing heartbeat or handoff, including an uncertain commit.
                await ref.update(update, option=fs_types.LastUpdateOption(snap.update_time))
                break
            except (FailedPrecondition, NotFound):
                return None
            except Aborted:
                if attempt == 4:
                    raise
                await asyncio.sleep(random.uniform(.02, .08) * 2 ** attempt)
        return _doc_to_session({**data, **update}, session_id)
