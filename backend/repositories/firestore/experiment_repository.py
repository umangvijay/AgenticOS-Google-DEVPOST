"""Firestore experiment repository (Phase 13 — parity).

The SAME contract as the SQLite ExperimentRepository against Firestore:
durable experiments with CAS state transitions inside async transactions
enforcing the SHARED state-machine/permission tables (both backends
cannot drift). Deterministic identity: experiments/{experiment_id}.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now
from backend.models.experiment import (
    Experiment,
    ExperimentKind,
    ExperimentState,
    ExperimentTransitionError,
    experiment_transition_valid,
)
from backend.repositories.experiment_repository import new_experiment_id

logger = logging.getLogger(__name__)

COLLECTION = "experiments"


def _doc_to_experiment(data: Dict[str, Any], experiment_id: str) -> Experiment:
    return Experiment(
        experiment_id=experiment_id,
        kind=ExperimentKind(data.get("kind") or "prompt"),
        target=data.get("target") or "",
        hypothesis=data.get("hypothesis") or "",
        expected_benefit=data.get("expected_benefit") or "",
        baseline_ref=data.get("baseline_ref") or "",
        candidate_ref=data.get("candidate_ref") or "",
        candidate_diff=data.get("candidate_diff") or "",
        dataset_ref=data.get("dataset_ref") or "",
        dataset_hash=data.get("dataset_hash") or "",
        grading_hash=data.get("grading_hash") or "",
        thresholds_json=data.get("thresholds_json") or "{}",
        state=ExperimentState(data.get("state") or "PROPOSED"),
        state_version=int(data.get("state_version") or 1),
        results_json=data.get("results_json") or "{}",
        proposed_by=data.get("proposed_by") or "",
        evaluated_by=data.get("evaluated_by") or "",
        approved_by=data.get("approved_by") or "",
        activated_by=data.get("activated_by") or "",
        rolled_back_by=data.get("rolled_back_by") or "",
        cohort_size=(int(data["cohort_size"])
                     if data.get("cohort_size") is not None else None),
        promoted_dataset_hash=data.get("promoted_dataset_hash"),
        promoted_results_json=(
            json.dumps(data["promoted_results"])
            if isinstance(data.get("promoted_results"), dict)
            else data.get("promoted_results_json")),
        promoted_actor=data.get("promoted_actor"),
        deployment_version=data.get("deployment_version"),
        rollback_reason=data.get("rollback_reason"),
        created_at=int(data.get("created_at") or 0),
        updated_at=int(data.get("updated_at") or 0),
    )


def _experiment_to_doc(experiment: Experiment) -> Dict[str, Any]:
    row = experiment.to_row()
    row.pop("experiment_id", None)
    return row


class FirestoreExperimentRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_experiment(self, experiment: Experiment) -> Experiment:
        now = epoch_now()
        experiment.created_at = now
        experiment.updated_at = now
        db = await self._get_db()
        await db.collection(COLLECTION).document(experiment.experiment_id).create(
            _experiment_to_doc(experiment))
        return experiment

    async def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            experiment_id).get()
        if not snap.exists:
            return None
        return _doc_to_experiment(snap.to_dict() or {}, experiment_id)

    async def list_experiments(self, limit: int = 50) -> List[Experiment]:
        db = await self._get_db()
        rows = []
        async for snap in db.collection(COLLECTION).limit(limit * 2).stream():
            rows.append(_doc_to_experiment(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[:limit]

    async def transition(self, experiment_id: str, target: ExperimentState,
                         *, role: str, actor: str,
                         expected_version: Optional[int] = None,
                         actor_field: Optional[str] = None,
                         results: Optional[Dict[str, Any]] = None,
                         cohort_size: Optional[int] = None,
                         deployment_version: Optional[str] = None,
                         rollback_reason: Optional[str] = None,
                         promoted_evidence: Optional[Dict[str, Any]] = None,
                         ) -> Experiment:
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(experiment_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise ExperimentTransitionError("Experiment not found",
                                                ExperimentState.PROPOSED, 0)
            current = _doc_to_experiment(snap.to_dict() or {}, experiment_id)
            if not experiment_transition_valid(current.state, target, role):
                raise ExperimentTransitionError(
                    f"Invalid experiment transition {current.state.value} → "
                    f"{target.value} by role {role}", current.state,
                    current.state_version)
            if expected_version is not None and \
                    current.state_version != int(expected_version):
                raise ExperimentTransitionError(
                    f"Experiment version conflict for {experiment_id}: "
                    f"durable {current.state_version}, caller "
                    f"{expected_version}", current.state, current.state_version)
            update: Dict[str, Any] = {
                "state": target.value,
                "state_version": current.state_version + 1,
                "updated_at": now,
            }
            if actor_field and actor:
                update[actor_field] = actor
            if results is not None:
                update["results_json"] = json.dumps(results)
            if cohort_size is not None:
                update["cohort_size"] = int(cohort_size)
            if deployment_version is not None:
                update["deployment_version"] = deployment_version
            if rollback_reason is not None:
                update["rollback_reason"] = rollback_reason
            if promoted_evidence is not None:
                update.update({
                    "promoted_dataset_hash": promoted_evidence.get("dataset_hash"),
                    "promoted_results": promoted_evidence.get("results") or {},
                    "promoted_actor": promoted_evidence.get("actor"),
                    "deployment_version": promoted_evidence.get("deployment_version"),
                })
            transaction.update(ref, update)

        await apply(db.transaction())
        return await self.get_experiment(experiment_id)
