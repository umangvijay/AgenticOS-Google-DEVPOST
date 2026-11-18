"""Experiment repository (Phase 13) — durable self-improvement records.

Experiments survive restart; every state change is a CAS transition with
the acting role recorded, so concurrent operators produce one winner.
Both SQLite and Firestore implementations expose the SAME contract.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.experiment import (
    ALLOWED_EXPERIMENT_TRANSITIONS,
    Experiment,
    ExperimentKind,
    ExperimentState,
    ExperimentTransitionError,
    experiment_transition_valid,
)


def new_experiment_id() -> str:
    return f"exp_{uuid.uuid4().hex[:24]}"


_EXPERIMENT_COLUMNS = (
    "experiment_id", "kind", "target", "hypothesis", "expected_benefit",
    "baseline_ref", "candidate_ref", "candidate_diff", "dataset_ref",
    "dataset_hash", "grading_hash", "thresholds_json", "state",
    "state_version", "results_json", "manifest_json",
    "proposed_by", "evaluated_by",
    "approved_by", "activated_by", "rolled_back_by", "cohort_size",
    "promoted_dataset_hash", "promoted_results_json", "promoted_actor",
    "deployment_version", "rollback_reason", "created_at", "updated_at",
)


def _row_to_experiment(row) -> Experiment:
    d = dict(row)
    return Experiment(
        experiment_id=d["experiment_id"],
        kind=ExperimentKind(d.get("kind") or "prompt"),
        target=d.get("target") or "",
        hypothesis=d.get("hypothesis") or "",
        expected_benefit=d.get("expected_benefit") or "",
        baseline_ref=d.get("baseline_ref") or "",
        candidate_ref=d.get("candidate_ref") or "",
        candidate_diff=d.get("candidate_diff") or "",
        dataset_ref=d.get("dataset_ref") or "",
        dataset_hash=d.get("dataset_hash") or "",
        grading_hash=d.get("grading_hash") or "",
        thresholds_json=d.get("thresholds_json") or "{}",
        state=ExperimentState(d.get("state") or "PROPOSED"),
        state_version=int(d.get("state_version") or 1),
        results_json=d.get("results_json") or "{}",
        manifest_json=d.get("manifest_json") or "{}",
        proposed_by=d.get("proposed_by") or "",
        evaluated_by=d.get("evaluated_by") or "",
        approved_by=d.get("approved_by") or "",
        activated_by=d.get("activated_by") or "",
        rolled_back_by=d.get("rolled_back_by") or "",
        cohort_size=(int(d["cohort_size"]) if d.get("cohort_size") is not None else None),
        promoted_dataset_hash=d.get("promoted_dataset_hash"),
        promoted_results_json=d.get("promoted_results_json"),
        promoted_actor=d.get("promoted_actor"),
        deployment_version=d.get("deployment_version"),
        rollback_reason=d.get("rollback_reason"),
        created_at=int(d.get("created_at") or 0),
        updated_at=int(d.get("updated_at") or 0),
    )


class ExperimentRepository:
    """SQLite implementation — the reference contract."""

    def __init__(self, db):
        self.db = db

    async def create_experiment(self, experiment: Experiment) -> Experiment:
        now = epoch_now()
        experiment.created_at = now
        experiment.updated_at = now
        row = experiment.to_row()
        cols = list(_EXPERIMENT_COLUMNS)
        await self.db.execute(
            f"INSERT INTO experiments ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(row[c] for c in cols))
        await self.db.commit()
        return experiment

    async def get_experiment(self, experiment_id: str) -> Optional[Experiment]:
        cursor = await self.db.execute(
            "SELECT * FROM experiments WHERE experiment_id=?", (experiment_id,))
        row = await cursor.fetchone()
        return _row_to_experiment(row) if row else None

    async def list_experiments(self, limit: int = 50) -> List[Experiment]:
        cursor = await self.db.execute(
            "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?",
            (limit,))
        return [_row_to_experiment(r) for r in await cursor.fetchall()]

    async def record_exposure(self, experiment_id: str, subject_id: str, *,
                              variant: str = "candidate",
                              max_cohort_size: int = 50) -> bool:
        """§13.17: durable admission — returns False when the ABSOLUTE
        cohort cap is reached (the hash fraction allocates approximately;
        this enforces the exact finite cap). Idempotent per subject."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM experiment_exposures WHERE experiment_id=? AND "
                "subject_id=?", (experiment_id, subject_id))
            if await cursor.fetchone():
                return True   # already exposed (idempotent)
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM experiment_exposures WHERE "
                "experiment_id=? AND variant=?", (experiment_id, variant))
            count = int((await cursor.fetchone())["n"])
            if variant == "candidate" and count >= int(max_cohort_size):
                return False
            await conn.execute(
                "INSERT INTO experiment_exposures "
                "(experiment_id, subject_id, variant, assigned_at) "
                "VALUES (?, ?, ?, ?)", (experiment_id, subject_id, variant, now))
        return True

    async def exposure_count(self, experiment_id: str, *,
                             variant: str = "candidate") -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS n FROM experiment_exposures WHERE "
            "experiment_id=? AND variant=?", (experiment_id, variant))
        return int((await cursor.fetchone())["n"])

    async def active_for_target(self, target: str) -> List[Experiment]:
        """§13.21: query ACTIVE experiments by target DIRECTLY — never a
        bounded scan of a general list that can hide older rows."""
        cursor = await self.db.execute(
            "SELECT * FROM experiments WHERE target=? AND state IN "
            "('SHADOW','CANARY') ORDER BY created_at", (target,))
        return [_row_to_experiment(r) for r in await cursor.fetchall()]

    async def record_shadow_observation(
        self, experiment_id: str, subject_id: str, *,
        candidate_version_id: str, outcome: str,
        latency_ms: Optional[float] = None, token_units: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """§13.16: a MEASURED shadow observation — recorded candidate
        execution (never user-served). Idempotent per
        (experiment, subject)."""
        import json as _json
        import uuid as _uuid
        now = epoch_now()
        observation_id = f"sho_{_uuid.uuid4().hex[:20]}"
        await self.db.execute(
            """INSERT OR IGNORE INTO shadow_observations
               (observation_id, experiment_id, subject_id,
                candidate_version_id, outcome, latency_ms, token_units,
                detail_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, experiment_id, subject_id,
             candidate_version_id, outcome, latency_ms, token_units,
             _json.dumps(detail or {}), now))
        await self.db.commit()
        return {"observation_id": observation_id, "experiment_id": experiment_id,
                "subject_id": subject_id,
                "candidate_version_id": candidate_version_id,
                "outcome": outcome, "latency_ms": latency_ms,
                "token_units": token_units, "recorded_at": now}

    async def shadow_observation_count(self, experiment_id: str) -> int:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS n FROM shadow_observations WHERE "
            "experiment_id=?", (experiment_id,))
        return int((await cursor.fetchone())["n"])

    async def shadow_summary(self, experiment_id: str) -> Dict[str, Any]:
        """§13.18: shadow-stage results — measured observations with mean
        latency and outcome distribution."""
        import json as _json
        cursor = await self.db.execute(
            "SELECT outcome, latency_ms, token_units, detail_json FROM "
            "shadow_observations WHERE experiment_id=? ORDER BY recorded_at",
            (experiment_id,))
        rows = [dict(r) for r in await cursor.fetchall()]
        total = len(rows)
        outcomes: Dict[str, int] = {}
        latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
        for r in rows:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
        return {
            "observations": total,
            "outcomes": outcomes,
            "mean_latency_ms": (round(sum(latencies) / len(latencies), 1)
                                if latencies else None),
            "token_units_total": sum(r["token_units"] or 0 for r in rows),
        }

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
        """CAS transition with the acting role + actor recorded (§13.4).
        `actor_field` names which permission slot the actor lands in
        (proposed_by / evaluated_by / approved_by / activated_by /
        rolled_back_by); promoted_evidence pins dataset+results+actor+
        deployment version at promotion time."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM experiments WHERE experiment_id=?",
                (experiment_id,))
            row = await cursor.fetchone()
            if not row:
                raise ExperimentTransitionError("Experiment not found",
                                                ExperimentState.PROPOSED, 0)
            current = _row_to_experiment(row)
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
            sets = ["state=?", "state_version=?", "updated_at=?"]
            args: List[Any] = [target.value, current.state_version + 1, now]
            if actor_field and actor:
                sets.append(f"{actor_field}=?")
                args.append(actor)
            if results is not None:
                sets.append("results_json=?")
                args.append(json.dumps(results))
            if cohort_size is not None:
                sets.append("cohort_size=?")
                args.append(int(cohort_size))
            if deployment_version is not None:
                sets.append("deployment_version=?")
                args.append(deployment_version)
            if rollback_reason is not None:
                sets.append("rollback_reason=?")
                args.append(rollback_reason)
            if promoted_evidence is not None:
                sets.extend(["promoted_dataset_hash=?",
                             "promoted_results_json=?", "promoted_actor=?",
                             "deployment_version=?"])
                args.extend([promoted_evidence.get("dataset_hash"),
                             json.dumps(promoted_evidence.get("results") or {}),
                             promoted_evidence.get("actor"),
                             promoted_evidence.get("deployment_version")])
            args.extend([experiment_id, current.state_version])
            cursor = await conn.execute(
                f"UPDATE experiments SET {', '.join(sets)} "
                "WHERE experiment_id=? AND state_version=?", tuple(args))
            if not cursor.rowcount:
                raise ExperimentTransitionError(
                    f"Concurrent experiment change for {experiment_id}",
                    current.state, current.state_version)
        return await self.get_experiment(experiment_id)


_repo: Optional[ExperimentRepository] = None


def set_experiment_repo(repo) -> None:
    global _repo
    _repo = repo


def get_experiment_repo() -> Optional[ExperimentRepository]:
    return _repo
