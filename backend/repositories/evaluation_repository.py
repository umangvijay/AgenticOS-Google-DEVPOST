"""Evaluation repository (Phase 12 §12.11/§12.12 — corrected per review).

Durable evaluation records with an IMMUTABLE-history contract:

- Create-or-get idempotency: the identity is (run_id, evaluator_version).
  A duplicate terminal event returns the ORIGINAL record (created=False)
  instead of creating a second one — worker crashes and redelivery
  neither lose evaluations nor inflate sample counts.
- Create-only results: an existing record's material contents are never
  rewritten in place. An intentional re-evaluation with a NEW scorer
  version creates a NEW record LINKED to the previous one
  (previous_evaluation_id).
- Indexed columns and the full record cannot disagree: rows are
  inserted once with every indexed column derived from the same dict
  that is serialized into record_json; no update path exists.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now


class EvaluationRepository:
    def __init__(self, db):
        self.db = db

    async def save_evaluation(self, record: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Create-or-get by (run_id, evaluator_version). Returns
        (record, created): on a duplicate delivery the ORIGINAL record is
        returned unchanged (§12.11/§12.12); on a NEW evaluator version a
        linked revision is created."""
        now = epoch_now()
        record.setdefault("created_at_epoch", now)

        existing = await self.get_for_identity(
            record.get("run_id"), record.get("evaluator_version", "1.0.0"))
        if existing is not None:
            existing_record = existing.get("record") or {}
            # Return the ORIGINAL — the caller must not overwrite history.
            return existing_record, False

        # §12.11: an intentional re-evaluation (new scorer version) links
        # to the latest existing record for the same run.
        if not record.get("previous_evaluation_id"):
            prior = await self.get_for_identity(
                record.get("run_id"), None, latest_any=True)
            if prior is not None and \
                    prior.get("evaluator_version") != record.get("evaluator_version"):
                record["previous_evaluation_id"] = prior["evaluation_id"]

        await self.db.execute(
            """
            INSERT INTO evaluation_records
                (evaluation_id, run_id, workflow_id, agent_name, model,
                 task_type, success, recovery_attempts, successful_recoveries,
                 tool_calls, unknown_tool_call_rate, invalid_argument_rate,
                 tool_schema_violation_rate, latency_ms, token_usage_total,
                 token_usage_input, token_usage_output,
                 evaluator_version, prompt_version, fixture_hash,
                 effect_states_json, criterion_results_json,
                 evidence_references_json, confidence_limitations_json,
                 previous_evaluation_id,
                 created_at_epoch, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (record.get("evaluation_id"), record.get("run_id"),
             record.get("workflow_id"), record.get("agent_name"),
             record.get("model"), record.get("task_type"),
             1 if record.get("success") else 0,
             record.get("recovery_attempts", 0),
             record.get("successful_recoveries", 0),
             record.get("tool_calls", 0),
             _number_or_none(record.get("unknown_tool_call_rate")),
             _number_or_none(record.get("invalid_argument_rate")),
             _number_or_none(record.get("tool_schema_violation_rate")),
             _number_or_none(record.get("latency_ms")),
             _int_or_none(record.get("token_usage_total")),
             _int_or_none(record.get("token_usage_input")),
             _int_or_none(record.get("token_usage_output")),
             record.get("evaluator_version", "1.0.0"),
             record.get("prompt_version", "1.0.0"),
             record.get("fixture_hash"),
             json.dumps(record.get("effect_states") or {}),
             json.dumps(record.get("criterion_results") or []),
             json.dumps(record.get("evidence_references") or []),
             json.dumps(record.get("confidence_limitations") or []),
             record.get("previous_evaluation_id"),
             record.get("created_at_epoch", now),
             json.dumps(record)))
        await self.db.commit()
        return record, True

    async def get_for_identity(self, run_id: Optional[str],
                               evaluator_version: Optional[str],
                               latest_any: bool = False) -> Optional[Dict[str, Any]]:
        """The record for (run_id, evaluator_version); with latest_any, the
        newest record for the run regardless of scorer version."""
        if run_id is None:
            return None
        if latest_any:
            cursor = await self.db.execute(
                "SELECT * FROM evaluation_records WHERE run_id=? "
                "ORDER BY created_at_epoch DESC LIMIT 1", (run_id,))
        elif evaluator_version is None:
            return None
        else:
            cursor = await self.db.execute(
                "SELECT * FROM evaluation_records WHERE run_id=? AND "
                "evaluator_version=? LIMIT 1", (run_id, evaluator_version))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["record"] = json.loads(d.pop("record_json", "{}"))
        return d

    async def get_evaluation(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM evaluation_records WHERE evaluation_id=?",
            (evaluation_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["record"] = json.loads(d.pop("record_json", "{}"))
        return d

    async def evaluations_for_run(self, run_id: str, *, limit: int = 50,
                                  offset: int = 0) -> List[Dict[str, Any]]:
        """§12.30: bounded retrieval with STABLE ordering
        (created_at_epoch, evaluation_id) — pagination never reorders."""
        cursor = await self.db.execute(
            "SELECT * FROM evaluation_records WHERE run_id=? "
            "ORDER BY created_at_epoch, evaluation_id LIMIT ? OFFSET ?",
            (run_id, limit, offset))
        rows = []
        for r in await cursor.fetchall():
            d = dict(r)
            try:
                d["record"] = json.loads(d.pop("record_json", "{}"))
            except ValueError:
                d["record"] = {}
            rows.append(d)
        return rows

    async def evaluation_summary(self, limit: int = 100) -> Dict[str, Any]:
        """§12.29: the aggregate selects the SAME recent-record window the
        API advertises — the LIMIT applies to the windowed subquery, not
        after aggregation (parity with the Firestore implementation)."""
        cursor = await self.db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes, "
            "AVG(latency_ms) as avg_latency, "
            "SUM(token_usage_total) as total_tokens, "
            "SUM(recovery_attempts) as total_recoveries "
            "FROM (SELECT * FROM evaluation_records "
            "      ORDER BY created_at_epoch DESC LIMIT ?)", (limit,))
        row = await cursor.fetchone()
        if not row or not row["total"]:
            return {"total": 0, "success_rate": 0, "avg_latency_ms": 0,
                    "total_tokens": 0, "total_recoveries": 0}
        total = row["total"]
        return {
            "total": total,
            "success_rate": round(row["successes"] / total, 3) if total else 0,
            "avg_latency_ms": round(row["avg_latency"] or 0, 1),
            "total_tokens": row["total_tokens"] or 0,
            "total_recoveries": row["total_recoveries"] or 0,
        }


def _number_or_none(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _int_or_none(value: Any) -> Optional[int]:
    return None if value is None else int(value)


_repo: Optional[EvaluationRepository] = None


def set_evaluation_repo(repo) -> None:
    global _repo
    _repo = repo


def get_evaluation_repo() -> Optional[EvaluationRepository]:
    return _repo
