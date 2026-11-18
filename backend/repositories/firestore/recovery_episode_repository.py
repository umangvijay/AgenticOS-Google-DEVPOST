"""Firestore recovery-episode repository (Phase 11 §11.2–11.8 — parity).

The SAME contract as the SQLite RecoveryEpisodeRepository against
Firestore:

- Deterministic document identity: recovery_episodes/{episode_id}.
- §11.7 ownership claims are a CAS inside an async transaction: the
  lease-expiry check, the generation increment, and the write commit
  atomically — concurrent workers produce ONE winner.
- §11.3 transitions enforce the SHARED state-machine tables imported
  from the SQLite module (both backends cannot drift) with
  expected-state/version CAS.
- Query methods sort client-side: no composite-index prerequisite.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now
from backend.repositories.recovery_episode_repository import (
    TERMINAL_RECOVERY_STATES, RecoveryState, RecoveryTransitionError,
    new_episode_id, recovery_transition_valid,
)

logger = logging.getLogger(__name__)

COLLECTION = "recovery_episodes"

_SCALARS = (
    "episode_id", "tenant_id", "actor_id", "command_id", "run_id", "task_id",
    "failed_step_id", "goal_digest", "failure_code", "failure_category",
    "provider_status", "proposal_action", "proposal_digest",
    "proposal_explanation", "policy_revision", "policy_result",
    "policy_reason", "owner_worker_id", "owner_epoch", "owner_generation",
    "lease_expires_at", "state", "state_version", "deadline_at",
    "attempt_count", "model_calls", "tool_calls", "verification_calls",
    "budget_reservation_id", "logical_action_id", "effect_id",
    "connector_revision_id", "credential_binding_id", "verification_verdict",
    "incident_id", "created_at", "updated_at",
)

_LISTS = ("acceptance_criteria", "allowed_scope", "evidence_references",
          "verification_evidence")
_DICTS = ("proposal_arguments",)


def _doc_to_episode(data: Dict[str, Any], episode_id: str) -> Dict[str, Any]:
    d = {k: data.get(k) for k in _SCALARS}
    d["episode_id"] = episode_id
    for name in _LISTS:
        d[name] = list(data.get(name) or [])
    for name in _DICTS:
        d[name] = dict(data.get(name) or {})
    return d


def _episode_to_doc(episode: Dict[str, Any]) -> Dict[str, Any]:
    doc = {k: episode.get(k) for k in _SCALARS if k != "episode_id"}
    for name in _LISTS:
        doc[name] = list(episode.get(name) or [])
    for name in _DICTS:
        doc[name] = dict(episode.get(name) or {})
    return doc


_OUTBOX_INTENT_STATES = frozenset({
    RecoveryState.PROPOSED, RecoveryState.ESCALATED,
    RecoveryState.VERIFIED_RECOVERY, RecoveryState.RECOVERY_FAILED,
})


class FirestoreRecoveryEpisodeRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_episode(self, episode: Dict[str, Any]) -> Dict[str, Any]:
        now = epoch_now()
        episode.setdefault("episode_id", new_episode_id())
        episode.setdefault("created_at", now)
        episode.setdefault("updated_at", now)
        episode.setdefault("state_version", 1)
        episode.setdefault("state", RecoveryState.DETECTED.value)
        episode.setdefault("owner_generation", 0)
        episode.setdefault("attempt_count", 0)
        episode.setdefault("model_calls", 0)
        episode.setdefault("tool_calls", 0)
        episode.setdefault("verification_calls", 0)
        db = await self._get_db()

        # §12.5: atomic creation/dedup — a deterministic ACTIVE-episode
        # anchor serializes concurrent creation for the same (run, task);
        # the anchor is deleted when the episode reaches a terminal state
        # (a NEW failure round may then create a fresh episode).
        active_ref = db.collection("recovery_active_episodes").document(
            f"{episode.get('run_id')}__{episode.get('task_id')}")

        @fs_types.async_transactional
        async def create(transaction):
            anchor = await active_ref.get(transaction=transaction)
            if anchor.exists:
                existing_id = (anchor.to_dict() or {}).get("episode_id")
                snap = await db.collection(COLLECTION).document(
                    existing_id).get(transaction=transaction)
                if snap.exists:
                    data = snap.to_dict() or {}
                    if data.get("state") not in {
                            s.value for s in TERMINAL_RECOVERY_STATES}:
                        return _doc_to_episode(data, existing_id)
                # Anchor pointed at a terminal episode — rebind to the new.
                transaction.set(active_ref, {"episode_id": episode["episode_id"]})
                transaction.create(db.collection(COLLECTION).document(
                    episode["episode_id"]), _episode_to_doc(episode))
                return episode
            transaction.set(active_ref, {"episode_id": episode["episode_id"]})
            transaction.create(db.collection(COLLECTION).document(
                episode["episode_id"]), _episode_to_doc(episode))
            return episode

        from backend.repositories.firestore.txn_retry import run_transaction
        return await run_transaction(db, create)

    async def get_episode(self, episode_id: str) -> Optional[Dict[str, Any]]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            episode_id).get()
        if not snap.exists:
            return None
        return _doc_to_episode(snap.to_dict() or {}, episode_id)

    async def get_episode_for_task(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        query = (db.collection(COLLECTION).where("run_id", "==", run_id)
                 .where("task_id", "==", task_id).limit(10))
        best: Optional[Dict[str, Any]] = None
        async for snap in query.stream():
            ep = _doc_to_episode(snap.to_dict() or {}, snap.id)
            if ep.get("state") in {s.value for s in TERMINAL_RECOVERY_STATES}:
                continue
            if best is None or (ep.get("created_at") or 0) > (best.get("created_at") or 0):
                best = ep
        return best

    async def claim_episode(self, episode_id: str, worker_id: str,
                            lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        """§11.7: atomic ownership claim — lease-expiry CAS + generation
        increment inside ONE transaction; concurrent claims → one winner."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(episode_id)

        @fs_types.async_transactional
        async def claim(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            if data.get("state") in {s.value for s in TERMINAL_RECOVERY_STATES}:
                return None
            lease_expires_at = data.get("lease_expires_at")
            owner = data.get("owner_worker_id")
            if lease_expires_at is not None and int(lease_expires_at) > now \
                    and owner != worker_id:
                return None   # another worker holds a live lease
            new_gen = int(data.get("owner_generation") or 0) + 1
            transaction.update(ref, {
                "owner_worker_id": worker_id,
                "owner_generation": new_gen,
                "lease_expires_at": now + lease_seconds,
                "updated_at": now,
            })
            return True

        if await claim(db.transaction()) is not True:
            return None
        return await self.get_episode(episode_id)

    async def transition(self, episode_id: str, target: RecoveryState, *,
                         actor: str = "system",
                         expected_state: Optional[RecoveryState] = None,
                         expected_version: Optional[int] = None,
                         evidence_reference: Optional[str] = None,
                         error_code: Optional[str] = None,
                         expected_owner_generation: Optional[int] = None,
                         require_live_lease: bool = False) -> Dict[str, Any]:
        """§12.5 parity: protected execution states require the CURRENT
        owner with a live lease — same rules as the SQLite reference."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(episode_id)
        _PROTECTED = {RecoveryState.EXECUTING, RecoveryState.VERIFYING,
                      RecoveryState.VERIFIED_RECOVERY,
                      RecoveryState.RECOVERY_FAILED}

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise RecoveryTransitionError("Episode not found",
                                              RecoveryState.DETECTED, 0)
            data = snap.to_dict() or {}
            current = RecoveryState(data.get("state", RecoveryState.DETECTED.value))
            version = int(data.get("state_version") or 1)
            if not recovery_transition_valid(current, target, actor):
                raise RecoveryTransitionError(
                    f"Invalid recovery transition {current.value} → "
                    f"{target.value} by {actor}", current, version)
            if expected_state is not None and current is not expected_state:
                raise RecoveryTransitionError(
                    f"Expected state {expected_state.value}, current {current.value}",
                    current, version)
            if expected_version is not None and version != int(expected_version):
                raise RecoveryTransitionError(
                    f"Version conflict for {episode_id}: durable {version}, "
                    f"caller {expected_version}", current, version)
            if (require_live_lease or target in _PROTECTED
                    or current in _PROTECTED):
                owner = data.get("owner_worker_id") or ""
                lease = data.get("lease_expires_at")
                if not owner:
                    raise RecoveryTransitionError(
                        f"Episode {episode_id} has no recovery owner — claim "
                        "it first (§12.5)", current, version)
                if lease is None or int(lease) <= now:
                    raise RecoveryTransitionError(
                        f"Recovery lease for {episode_id} expired — a stale "
                        "owner cannot commit (§12.5)", current, version)
                if expected_owner_generation is not None and \
                        int(data.get("owner_generation") or 0) != int(expected_owner_generation):
                    raise RecoveryTransitionError(
                        f"Ownership generation conflict for {episode_id}: "
                        f"durable {data.get('owner_generation')}, caller "
                        f"{expected_owner_generation}", current, version)
            update: Dict[str, Any] = {
                "state": target.value, "state_version": version + 1,
                "updated_at": now,
            }
            if evidence_reference:
                update["evidence_references"] = list(
                    data.get("evidence_references") or []) + [evidence_reference]
            if error_code:
                update["error_code"] = error_code
            transaction.update(ref, update)
            if target in TERMINAL_RECOVERY_STATES:
                # §12.5: a terminal episode frees the (run, task) pair for a
                # NEW failure round.
                transaction.delete(db.collection("recovery_active_episodes")
                                   .document(f"{data.get('run_id')}__"
                                             f"{data.get('task_id')}"))
            # §12.5: durable transitions AND outbox intents (parity with
            # SQLite) — the same state set commits an outbox doc in the
            # SAME transaction.
            if target in _OUTBOX_INTENT_STATES:
                import uuid as _uuid
                transaction.create(
                    db.collection("outbox_events").document(
                        f"evt_{_uuid.uuid4().hex[:20]}"), {
                            "command_id": None,
                            "aggregate_id": episode_id,
                            "aggregate_version": version + 1,
                            "event_type": f"recovery.{target.value}",
                            "schema_version": 1,
                            "payload_json": json.dumps({
                                "episode_id": episode_id,
                                "tenant_id": data.get("tenant_id"),
                                "run_id": data.get("run_id"),
                                "task_id": data.get("task_id"),
                                "from_state": current.value,
                                "to_state": target.value,
                                "actor": actor, "error_code": error_code,
                            }),
                            "created_at": now, "available_at": now,
                            "publish_attempts": 0,
                        })

        await apply(db.transaction())
        return await self.get_episode(episode_id)

    async def abandoned_episodes(self, lease_grace_seconds: int = 120,
                                 limit: int = 50) -> List[Dict[str, Any]]:
        """§11.8: episodes whose lease expired while still active."""
        db = await self._get_db()
        cutoff = epoch_now() - lease_grace_seconds
        active_states = {s.value for s in RecoveryState
                         if s not in TERMINAL_RECOVERY_STATES}
        rows: List[Dict[str, Any]] = []
        # The active-state set is closed (12 known states): one equality
        # query per state — no composite index needed.
        for state in sorted(active_states):
            query = (db.collection(COLLECTION).where("state", "==", state)
                     .limit(limit))
            async for snap in query.stream():
                ep = _doc_to_episode(snap.to_dict() or {}, snap.id)
                lease = ep.get("lease_expires_at")
                if lease is None or int(lease) <= cutoff:
                    rows.append(ep)
        rows.sort(key=lambda e: e.get("created_at") or 0)
        return rows[:limit]

    async def increment_counters(self, episode_id: str, *,
                                 model_calls: int = 0, tool_calls: int = 0,
                                 verification_calls: int = 0,
                                 attempt_count: int = 0) -> None:
        """Persistent accounting (§11.9): field increments are atomic in
        Firestore — no read-modify-write race."""
        ref = (await self._get_db()).collection(COLLECTION).document(episode_id)
        updates: Dict[str, Any] = {"updated_at": epoch_now()}
        for field, delta in (("model_calls", model_calls),
                             ("tool_calls", tool_calls),
                             ("verification_calls", verification_calls),
                             ("attempt_count", attempt_count)):
            if delta:
                updates[field] = fs_types.Increment(delta)
        await ref.update(updates)
