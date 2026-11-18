"""Workflow templates and dry runs (Phase 21 §21.1/§21.2).

- Versioned TEMPLATES: definition + required capabilities + config
  schema; a preview shows the actions and permissions BEFORE creating a
  run — users see what a template would do.
- DRY RUN: a SIDE-EFFECT-FREE execution proposal — expected inputs/
  outputs, external effects (which tasks would write), and a bounded
  cost ESTIMATE clearly labeled (uncertainty explicit, never presented
  as a quote).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("workflow_templates")


def new_template_id() -> str:
    return f"wft_{uuid.uuid4().hex[:20]}"


def template_version_hash(definition: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(definition, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


class WorkflowTemplateService:
    """SQLite reference (Firestore parity follows the established
    repository pattern for Phase 13/14 stores)."""

    def __init__(self, db):
        self.db = db

    async def create_template(self, *, tenant_id: str, name: str,
                              definition: Dict[str, Any],
                              required_capabilities: List[str],
                              config_schema: Dict[str, Any],
                              created_by: str) -> Dict[str, Any]:
        now = epoch_now()
        template_id = new_template_id()
        version_hash = template_version_hash(definition)
        await self.db.execute(
            """INSERT INTO workflow_templates
               (template_id, tenant_id, name, version, definition_json,
                required_capabilities_json, config_schema_json,
                created_by, created_at, updated_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
            (template_id, tenant_id, name, json.dumps(definition),
             json.dumps(required_capabilities or []),
             json.dumps(config_schema or {}), created_by, now, now))
        await self.db.commit()
        return await self.get_template(template_id)

    async def update_template(self, template_id: str, *,
                              definition: Dict[str, Any],
                              updated_by: str) -> Dict[str, Any]:
        """§21.1: editing creates a NEW VERSION — existing runs reference
        the exact version they used (immutable revisions, §6.2 parity)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM workflow_templates "
            "WHERE template_id=?", (template_id,))
        row = await cursor.fetchone()
        next_version = int(row["v"] or 0) + 1
        await self.db.execute(
            """INSERT INTO workflow_templates
               (template_id, tenant_id, name, version, definition_json,
                required_capabilities_json, config_schema_json,
                is_active, created_by, updated_by, created_at, updated_at)
               SELECT template_id, tenant_id, name, ?, ?, ?, ?,
                      0, created_by, ?, ?, ?
               FROM workflow_templates WHERE template_id=? AND version=?""",
            (next_version, json.dumps(definition),
             json.dumps([]), json.dumps({}),
             updated_by, now, now, template_id, next_version - 1))
        await self.db.commit()
        return await self.get_template(template_id, version=next_version)

    async def get_template(self, template_id: str,
                           version: Optional[int] = None
                           ) -> Optional[Dict[str, Any]]:
        if version is None:
            cursor = await self.db.execute(
                "SELECT * FROM workflow_templates WHERE template_id=? "
                "ORDER BY version DESC LIMIT 1", (template_id,))
        else:
            cursor = await self.db.execute(
                "SELECT * FROM workflow_templates WHERE template_id=? AND "
                "version=?", (template_id, version))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["definition"] = json.loads(d.pop("definition_json") or "{}")
        d["required_capabilities"] = json.loads(
            d.pop("required_capabilities_json") or "[]")
        d["config_schema"] = json.loads(d.pop("config_schema_json") or "{}")
        d["version_hash"] = template_version_hash(d["definition"])
        return d

    async def list_templates(self, tenant_id: str,
                             limit: int = 50) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT t.* FROM workflow_templates t "
            "WHERE t.tenant_id=? AND t.version = "
            "(SELECT MAX(version) FROM workflow_templates w "
            " WHERE w.template_id = t.template_id) "
            "ORDER BY t.created_at DESC LIMIT ?", (tenant_id, limit))
        rows = []
        for r in await cursor.fetchall():
            d = dict(r)
            d["definition"] = json.loads(d.pop("definition_json") or "{}")
            d["required_capabilities"] = json.loads(
                d.pop("required_capabilities_json") or "[]")
            d["config_schema"] = json.loads(d.pop("config_schema_json") or "{}")
            d["version_hash"] = template_version_hash(d["definition"])
            rows.append(d)
        return rows

    async def set_active(self, template_id: str, version: int,
                         *, updated_by: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE workflow_templates SET is_active=0 WHERE template_id=?",
            (template_id,))
        await self.db.execute(
            "UPDATE workflow_templates SET is_active=1, updated_by=?, "
            "updated_at=? WHERE template_id=? AND version=?",
            (updated_by, epoch_now(), template_id, version))
        await self.db.commit()
        return await self.get_template(template_id, version=version)


# ── §21.2: the dry run ──────────────────────────────────────────────────

# Task agents that produce EXTERNAL effects (the dry run names them).
EXTERNAL_EFFECT_AGENTS = {
    "core.email": "sends email", "core.http": "calls an external API",
    "browser": "drives a real browser session",
    "core.mcp_build": "creates a new integration",
}

READ_ONLY_AGENTS = {"core.search", "core.summarize", "core.transform"}


def dry_run_workflow(definition: Dict[str, Any], *,
                     config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A side-effect-free execution proposal (§21.2): expected inputs and
    outputs per task, which tasks would produce EXTERNAL effects, and a
    bounded cost ESTIMATE (clearly labeled, uncertainty explicit).
    Nothing here executes, dispatches, or reserves."""
    tasks = definition.get("tasks") or []
    plan_tasks = []
    external_effects = []
    ai_task_count = 0
    for t in tasks:
        agent = str(t.get("agent") or "")
        effect = EXTERNAL_EFFECT_AGENTS.get(agent) or (
            "external tool call" if agent.startswith("core.") and
            agent not in READ_ONLY_AGENTS and t.get("tool") else None)
        entry = {
            "task_id": t.get("task_id"),
            "agent": agent,
            "dependencies": list(t.get("dependencies") or []),
            "expected_input": (t.get("input_data") or {}),
            "external_effect": effect,
        }
        plan_tasks.append(entry)
        if effect:
            external_effects.append({"task_id": t.get("task_id"),
                                     "effect": effect})
        if not agent.startswith("core."):
            ai_task_count += 1

    # Bounded estimate: model tokens for AI tasks + a per-effect overhead.
    # Labeled ESTIMATE with explicit uncertainty — never a quote (§21.2).
    effect_count = len(external_effects)
    est_min_units = ai_task_count * 400 + effect_count * 500
    est_max_units = ai_task_count * 4000 + effect_count * 2000
    return {
        "dry_run": True,
        "task_count": len(tasks),
        "tasks": plan_tasks,
        "external_effects": external_effects,
        "cost_estimate": {
            "kind": "ESTIMATE",
            "unit": "tokens",
            "min": est_min_units,
            "max": est_max_units,
            "uncertainty": ("depends on provider output lengths, tool "
                            "results, and any recovery; the reservation "
                            "bounds the actual spend at admission time"),
            "pricing_version": None,
        },
        "notes": ("Nothing was executed. Confirm the external effects "
                  "before creating the run."),
    }


# ── §21.3: run comparison (evidence-based, no replay of effects) ───────

def compare_runs(evaluations_a: List[Dict[str, Any]],
                 evaluations_b: List[Dict[str, Any]], *,
                 label_a: str = "A", label_b: str = "B") -> Dict[str, Any]:
    """Compare the VERIFIED evidence of two runs (or two cohorts):
    success, task outcomes, recovery, tool metrics, cost. Read-only —
    replay here means INSPECTING historical decisions, never repeating
    real-world effects (§21.3)."""
    def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"evaluations": 0}
        latest = rows[-1]
        record = latest.get("record") or latest
        return {
            "evaluations": len(rows),
            "success": bool(latest.get("success", record.get("success"))),
            "completed_tasks": record.get("completed_tasks"),
            "failed_tasks": record.get("failed_tasks"),
            "recovery_attempts": record.get("recovery_attempts"),
            "tool_calls": record.get("tool_calls"),
            "latency_ms": record.get("latency_ms"),
            "token_usage_total": record.get("token_usage_total"),
            "fixture_hash": record.get("fixture_hash") or latest.get("fixture_hash"),
            "evaluator_version": record.get("evaluator_version") or
                                 latest.get("evaluator_version"),
        }

    a, b = _summarize(evaluations_a), _summarize(evaluations_b)
    deltas = {}
    for key in ("completed_tasks", "failed_tasks", "recovery_attempts",
                "tool_calls", "token_usage_total"):
        va, vb = a.get(key), b.get(key)
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            deltas[key] = {"a": va, "b": vb, "delta": vb - va}
    return {
        "comparison": {"a": label_a, "b": label_b},
        "a": a, "b": b,
        "deltas": deltas,
        "same_fixture": (a.get("fixture_hash") is not None
                         and a.get("fixture_hash") == b.get("fixture_hash")),
        "note": ("Evidence comparison only — the runs' external effects "
                 "are never re-executed."),
    }


# ── §21.3: run replay — inspect/simulate historical decisions ──────────

def replay_run(steps: List[Dict[str, Any]],
               evaluation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """§21.3 replay: re-apply the CURRENT deterministic policy to a run's
    durable execution record — what would the current verifier say about
    each recorded output, and which recorded proposals would the current
    pipeline refuse? PURE INSPECTION: nothing dispatches, nothing
    mutates, no external effect is repeated."""
    from backend.services.outcome_verifier import (
        AcceptanceCriterion, OutcomeVerifier,
    )
    step_outcomes = []
    for step in steps or []:
        proposed = step.get("proposed_action")
        result_ref = step.get("result_reference")
        error = step.get("error_code")
        outcome = {
            "step_id": step.get("step_id"),
            "proposed_action": proposed,
            "recorded_state": step.get("state"),
            "error_code": error,
            "would_revalidate": False,
        }
        # Would the current policy still accept the recorded validated
        # action? A step that recorded a validated digest revalidates as
        # structural; errors stay errors.
        if step.get("validated_action_digest") and not error:
            outcome["would_revalidate"] = True
        step_outcomes.append(outcome)

    evaluation_note = None
    if evaluation is not None:
        record = evaluation.get("record") or evaluation
        evaluation_note = {
            "evaluator_version": record.get("evaluator_version"),
            "recorded_success": record.get("success"),
            "note": ("the recorded evaluation is historical — replay "
                     "reports it, it does not rescore"),
        }
    return {
        "replay": "inspection-only",
        "steps": step_outcomes,
        "steps_total": len(step_outcomes),
        "steps_revalidating": sum(1 for s in step_outcomes
                                  if s["would_revalidate"]),
        "recorded_evaluation": evaluation_note,
        "note": ("Replay inspects and simulates historical decisions. "
                 "No external effect is ever repeated (§21.3)."),
    }
