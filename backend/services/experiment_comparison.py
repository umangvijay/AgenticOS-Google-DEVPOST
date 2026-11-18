"""Durable experiment comparisons (Phase 13 §13.13).

Replaces the unconditional 501 with a REAL comparison pipeline:

- The API accepts a comparison request and returns a DURABLE job
  identity (create-or-get per experiment revision — duplicate requests
  return the same job).
- Workers execute the pinned baseline/candidate cases with bounded
  concurrency, deadlines, and RESUMABLE per-case records: a crash
  halfway through resumes without rewriting completed evidence and
  without leaving the experiment permanently EVALUATING.
- Per-case records carry the case id, variant, verdict, and the raw
  judge/evaluator output; metrics are derived FROM the records (samples,
  success rate, constraint flags) in the shape the §13.12 typed gates
  consume.
- Failed comparisons requeue bounded times then park `exhausted`;
  cancelled comparisons stop cleanly.

The case EXECUTOR is injectable (`run_case(case, variant_ref) -> dict`):
in production it renders the pinned prompt version against the case
input through the model path; in tests it is deterministic. Without an
executor the job waits — it never fabricates results.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.experiment import ExperimentState


def new_comparison_id() -> str:
    return f"cmp_{uuid.uuid4().hex[:20]}"


class ComparisonError(Exception):
    pass


class ExperimentComparisonRepository:
    """SQLite reference (Firestore parity follows the established
    anchor pattern when the comparison worker goes to staging)."""

    def __init__(self, db):
        self.db = db

    async def enqueue(self, experiment_id: str, *, cases: List[Dict[str, Any]],
                      baseline_ref: str, candidate_ref: str,
                      revision_hash: str) -> Dict[str, Any]:
        """Create-or-get by (experiment_id, revision_hash): a repeated
        request for the SAME pinned comparison returns the SAME job."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT * FROM experiment_comparisons WHERE experiment_id=? AND "
            "revision_hash=?", (experiment_id, revision_hash))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        comparison_id = new_comparison_id()
        await self.db.execute(
            """INSERT INTO experiment_comparisons
               (comparison_id, experiment_id, revision_hash, state,
                state_version, baseline_ref, candidate_ref,
                cases_json, case_results_json, created_at, updated_at)
               VALUES (?, ?, ?, 'queued', 1, ?, ?, ?, '[]', ?, ?)""",
            (comparison_id, experiment_id, revision_hash, baseline_ref,
             candidate_ref, json.dumps(cases or []), now, now))
        await self.db.commit()
        return await self.get(comparison_id)

    async def get(self, comparison_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM experiment_comparisons WHERE comparison_id=?",
            (comparison_id,))
        row = await cursor.fetchone()
        return self._expand(dict(row)) if row else None

    @staticmethod
    def _expand(d: Dict[str, Any]) -> Dict[str, Any]:
        d["cases"] = json.loads(d.pop("cases_json") or "[]")
        d["case_results"] = json.loads(d.pop("case_results_json") or "[]")
        return d

    async def claim(self, worker_id: str, *, lease_seconds: int = 300,
                    batch: int = 1) -> List[Dict[str, Any]]:
        now = epoch_now()
        claimed = []
        cursor = await self.db.execute(
            "SELECT comparison_id, state_version FROM experiment_comparisons "
            "WHERE state='queued' OR (state='running' AND lease_expires_at<=?) "
            "ORDER BY created_at LIMIT ?", (now, batch))
        for row in await cursor.fetchall():
            cursor = await self.db.execute(
                "UPDATE experiment_comparisons SET state='running', "
                "owner_worker_id=?, lease_expires_at=?, "
                "state_version=state_version+1, updated_at=? "
                "WHERE comparison_id=? AND state_version=?",
                (worker_id, now + lease_seconds, now, row["comparison_id"],
                 row["state_version"]))
            if cursor.rowcount:
                claimed.append(row["comparison_id"])
        await self.db.commit()
        return [await self.get(cid) for cid in claimed]

    async def record_case(self, comparison_id: str, *, case_id: str,
                          variant: str, verdict: str,
                          detail: Optional[Dict[str, Any]] = None) -> None:
        """RESUMABLE per-case records: each completed case commits alone —
        a crash halfway keeps the finished evidence untouched."""
        now = epoch_now()
        row = await self.get(comparison_id)
        if row is None:
            raise ComparisonError("Comparison not found")
        results = [r for r in row["case_results"]
                   if not (r.get("case_id") == case_id
                           and r.get("variant") == variant)]
        results.append({"case_id": case_id, "variant": variant,
                        "verdict": verdict,
                        "detail": detail or {},
                        "recorded_at": now})
        await self.db.execute(
            "UPDATE experiment_comparisons SET case_results_json=?, "
            "updated_at=? WHERE comparison_id=?",
            (json.dumps(results), now, comparison_id))
        await self.db.commit()

    async def complete(self, comparison_id: str, *, owner_worker_id: str,
                       metrics: Dict[str, Any]) -> None:
        """Lease-fenced completion (§13.2 parity): only the current owner
        finishes the comparison."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT owner_worker_id, state FROM experiment_comparisons "
            "WHERE comparison_id=?", (comparison_id,))
        row = await cursor.fetchone()
        if not row or row["owner_worker_id"] != owner_worker_id or \
                row["state"] != "running":
            raise ComparisonError(
                "Stale worker: the comparison lease is no longer yours")
        await self.db.execute(
            "UPDATE experiment_comparisons SET state='completed', "
            "results_json=?, lease_expires_at=NULL, updated_at=? "
            "WHERE comparison_id=?", (json.dumps(metrics), now, comparison_id))
        await self.db.commit()

    async def fail(self, comparison_id: str, error: str, *,
                   max_attempts: int = 3) -> Dict[str, Any]:
        now = epoch_now()
        await self.db.execute(
            "UPDATE experiment_comparisons SET attempts=attempts+1, "
            "last_error=?, updated_at=? WHERE comparison_id=?",
            (error[:300], now, comparison_id))
        cursor = await self.db.execute(
            "SELECT attempts FROM experiment_comparisons WHERE comparison_id=?",
            (comparison_id,))
        attempts = int((await cursor.fetchone())["attempts"])
        state = "exhausted" if attempts >= max_attempts else "queued"
        await self.db.execute(
            "UPDATE experiment_comparisons SET state=?, lease_expires_at=NULL, "
            "owner_worker_id=NULL WHERE comparison_id=?",
            (state, comparison_id))
        await self.db.commit()
        return await self.get(comparison_id)

    async def cancel(self, comparison_id: str) -> None:
        await self.db.execute(
            "UPDATE experiment_comparisons SET state='cancelled', "
            "lease_expires_at=NULL, updated_at=? WHERE comparison_id=?",
            (epoch_now(), comparison_id))
        await self.db.commit()

    @staticmethod
    def compute_metrics(case_results: List[Dict[str, Any]], *,
                        variant: str, constraint_names: tuple = ()) -> Dict[str, Any]:
        """§13.12-shaped metrics from per-case records: samples, success
        fraction, and constraint flags (a constraint holds only if EVERY
        case held it)."""
        rows = [r for r in case_results if r.get("variant") == variant]
        total = len(rows)
        passed = sum(1 for r in rows if r.get("verdict") == "pass")
        metrics: Dict[str, Any] = {"samples": total}
        if total:
            metrics["success"] = round(passed / total, 4)
        for name in constraint_names:
            held = all((r.get("detail") or {}).get(f"constraint_{name}") is True
                       for r in rows) if rows else False
            metrics[f"constraint_{name}"] = bool(rows) and held
        return metrics


_repo: Optional[ExperimentComparisonRepository] = None


def set_comparison_repo(repo) -> None:
    global _repo
    _repo = repo


def get_comparison_repo() -> Optional[ExperimentComparisonRepository]:
    return _repo


async def drain_comparisons(worker_id: str, *, batch: int = 1,
                            run_case: Optional[Callable] = None,
                            experiment_repo=None) -> int:
    """§13.13 + §13.16: the comparison worker — executes pinned cases for
    BOTH variants, records each case durably, and completes with
    §13.12-shaped metrics. When the experiment is in SHADOW, every
    candidate execution is ALSO recorded as a MEASURED shadow observation
    (outcome + latency + cost) — 'shadow observed' requires recorded
    candidate execution, never a state label. The baseline serves users;
    nothing here is user-facing. `run_case(case, variant_ref) ->
    {verdict, detail}` is injected by the deployment (the model path);
    without one the job waits (never fabricated)."""
    repo = get_comparison_repo()
    if repo is None or run_case is None:
        return 0
    processed = 0
    for comp in await repo.claim(worker_id, batch=batch):
        try:
            exp = None
            if experiment_repo is not None:
                exp = await experiment_repo.get_experiment(
                    comp["experiment_id"])
            for case in comp["cases"]:
                for variant_ref, variant in (
                        (comp["baseline_ref"], "baseline"),
                        (comp["candidate_ref"], "candidate")):
                    # Skip cases already recorded (resume) — completed
                    # evidence is never rewritten.
                    done = any(r.get("case_id") == case.get("case_id")
                               and r.get("variant") == variant
                               for r in comp["case_results"])
                    if done:
                        continue
                    outcome = await run_case(case, variant_ref)
                    await repo.record_case(
                        comp["comparison_id"], case_id=case.get("case_id"),
                        variant=variant,
                        verdict=str(outcome.get("verdict", "needs_review")),
                        detail=outcome.get("detail") or {})
                    # §13.16: measured shadow observations for candidate
                    # executions on SHADOW experiments.
                    if (variant == "candidate" and exp is not None
                            and exp.state.value == "SHADOW"
                            and hasattr(experiment_repo,
                                        "record_shadow_observation")):
                        detail = outcome.get("detail") or {}
                        await experiment_repo.record_shadow_observation(
                            comp["experiment_id"], case.get("case_id"),
                            candidate_version_id=comp["candidate_ref"],
                            outcome=str(outcome.get("verdict",
                                                    "needs_review")),
                            latency_ms=detail.get("latency_ms"),
                            token_units=detail.get("token_units"),
                            detail=detail)
            # §13.12: constraint flags ride in the case details — the
            # names are the UNION across recorded cases.
            case_results = (await repo.get(comp["comparison_id"]))["case_results"]
            constraint_names = set()
            for r in case_results:
                for key in (r.get("detail") or {}):
                    if key.startswith("constraint_"):
                        constraint_names.add(key[len("constraint_"):])
            metrics = {}
            for variant in ("baseline", "candidate"):
                metrics[variant] = repo.compute_metrics(
                    case_results, variant=variant,
                    constraint_names=tuple(sorted(constraint_names)))
            await repo.complete(comp["comparison_id"],
                                owner_worker_id=worker_id, metrics=metrics)
            # §13.13: the experiment does NOT stay EVALUATING — the
            # completed comparison IS the evaluation: record the results
            # and advance PROPOSED → EVALUATED (role evaluator).
            if experiment_repo is not None:
                try:
                    exp = await experiment_repo.get_experiment(
                        comp["experiment_id"])
                    if exp is not None and exp.state.value == "PROPOSED":
                        await experiment_repo.transition(
                            comp["experiment_id"], ExperimentState.EVALUATING,
                            role="evaluator", actor=f"worker:{worker_id}",
                            actor_field="evaluated_by")
                        await experiment_repo.transition(
                            comp["experiment_id"], ExperimentState.EVALUATED,
                            role="evaluator", actor=f"worker:{worker_id}",
                            results=metrics)
                except Exception:
                    logger.debug("Experiment EVALUATED advance failed",
                                 exc_info=True)
            processed += 1
        except Exception as exc:
            await repo.fail(comp["comparison_id"], str(exc))
    return processed
