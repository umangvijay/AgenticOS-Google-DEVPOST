"""Firestore execution-step repository (Phase 10 §10.5/§10.6 — parity).

The SAME contract as the SQLite ExecutionStepRepository against Firestore:

- Deterministic document identity: execution_steps/{step_id}.
- CAS transitions run inside an async transaction (all reads precede
  writes; the callback may rerun — nothing inside it publishes or calls
  providers).
- State-machine rules and actor restrictions are the SHARED module-level
  tables imported from the SQLite module, so the two backends cannot
  drift.
- List queries sort client-side: no composite-index prerequisite, so a
  fresh deployment (or the emulator) works without pre-created indexes.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.repositories.execution_step_repository import (
    StepState, StepTransitionError, step_transition_valid,
)

logger = logging.getLogger(__name__)

COLLECTION = "execution_steps"

# Fields copied verbatim from the caller's step dict (scalar identity and
# evidence); list/dict fields store natively, no JSON blobs.
_SCALARS = (
    "step_id", "logical_action_id", "attempt_id", "command_id", "run_id",
    "task_id", "tenant_id", "actor_id", "sequence", "state", "state_version",
    "lease_generation", "plan_revision", "policy_revision",
    "connector_revision", "proposed_action", "validated_action_digest",
    "authorization_reference", "budget_reservation_id", "effect_id",
    "result_reference", "verification_reference", "error_code",
    "created_at", "updated_at", "deadline_at",
)


def _doc_to_step(data: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    d = {k: data.get(k) for k in _SCALARS}
    d["step_id"] = step_id
    d["observation_references"] = list(data.get("observation_references") or [])
    d["evidence_references"] = list(data.get("evidence_references") or [])
    return d


def _step_to_doc(step: Dict[str, Any]) -> Dict[str, Any]:
    doc = {k: step.get(k) for k in _SCALARS if k != "step_id"}
    doc["observation_references"] = list(step.get("observation_references") or [])
    doc["evidence_references"] = list(step.get("evidence_references") or [])
    return doc


class FirestoreExecutionStepRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        now = epoch_now()
        step.setdefault("created_at", now)
        step.setdefault("updated_at", now)
        step.setdefault("state_version", 1)
        step.setdefault("state", StepState.OBSERVING.value)
        db = await self._get_db()
        # create() (not set) — a concurrent duplicate surfaces as an error,
        # mirroring the SQLite PRIMARY KEY semantics.
        await db.collection(COLLECTION).document(step["step_id"]).create(
            _step_to_doc(step))
        return await self.get_step(step["step_id"])

    async def get_step(self, step_id: str) -> Optional[Dict[str, Any]]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            step_id).get()
        if not snap.exists:
            return None
        return _doc_to_step(snap.to_dict() or {}, step_id)

    async def _query_steps(self, where: tuple, limit: int) -> List[Dict[str, Any]]:
        db = await self._get_db()
        query = db.collection(COLLECTION)
        for field, value in where:
            query = query.where(field, "==", value)
        rows: List[Dict[str, Any]] = []
        async for snap in query.limit(limit * 4).stream():
            rows.append(_doc_to_step(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda d: (d.get("sequence") or 0, d.get("created_at") or 0))
        return rows[:limit]

    async def steps_for_task(self, run_id: str, task_id: str,
                             limit: int = 100) -> List[Dict[str, Any]]:
        return await self._query_steps(
            (("run_id", run_id), ("task_id", task_id)), limit)

    async def steps_for_run(self, run_id: str,
                            limit: int = 500) -> List[Dict[str, Any]]:
        return await self._query_steps((("run_id", run_id),), limit)

    async def steps_for_logical_action(self, logical_action_id: str) -> List[Dict[str, Any]]:
        return await self._query_steps(
            (("logical_action_id", logical_action_id),), 500)

    async def transition(self, step_id: str, target: StepState, *,
                         actor: str, expected_state: Optional[StepState] = None,
                         expected_version: Optional[int] = None,
                         evidence_reference: Optional[str] = None,
                         error_code: Optional[str] = None,
                         result_reference: Optional[str] = None) -> Dict[str, Any]:
        from google.cloud import firestore as fs_types
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(step_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise StepTransitionError("Step not found", StepState.FAILED, 0)
            data = snap.to_dict() or {}
            current = StepState(data.get("state", StepState.OBSERVING.value))
            version = int(data.get("state_version") or 1)
            if not step_transition_valid(current, target, actor):
                raise StepTransitionError(
                    f"Invalid step transition {current.value} → {target.value} "
                    f"by {actor}", current, version)
            if expected_state is not None and current is not expected_state:
                raise StepTransitionError(
                    f"Expected state {expected_state.value}, current {current.value}",
                    current, version)
            if expected_version is not None and version != int(expected_version):
                raise StepTransitionError(
                    f"Version conflict for {step_id}: durable {version}, "
                    f"caller {expected_version}", current, version)
            update: Dict[str, Any] = {
                "state": target.value, "state_version": version + 1,
                "updated_at": now,
            }
            if evidence_reference:
                update["evidence_references"] = list(
                    data.get("evidence_references") or []) + [evidence_reference]
            if error_code:
                update["error_code"] = error_code
            if result_reference:
                update["result_reference"] = result_reference
            transaction.update(ref, update)

        await apply(db.transaction())
        return await self.get_step(step_id)
