"""Durable execution-step repository (Phase 10 §10.5/§10.6).

One record per ReAct step with the §10.5 field set: logical action identity,
attempt identity, command/run/task/tenant IDs, policy revisions, evidence
references, effect ID, verification reference, and the §10.6 state machine.

State machine (§10.6): OBSERVING → PROPOSED → VALIDATING → [WAITING_*] →
READY → DISPATCHING → VERIFYING → VERIFIED | FAILED | OUTCOME_UNCERTAIN |
CANCELLED | SUPERSEDED. DISPATCHING → OUTCOME_UNCERTAIN is the appropriate
transition when an external action may have happened but the result was
lost — a retry must not convert that directly into another dispatch.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now


class StepState(str, Enum):
    OBSERVING = "OBSERVING"
    PROPOSED = "PROPOSED"
    VALIDATING = "VALIDATING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CREDENTIALS = "WAITING_CREDENTIALS"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


ALLOWED_STEP_TRANSITIONS: Dict[StepState, Dict[StepState, set]] = {
    StepState.OBSERVING: {
        StepState.PROPOSED: {"agent", "system"},
        StepState.CANCELLED: {"system", "operator"},
        StepState.SUPERSEDED: {"system"},
    },
    StepState.PROPOSED: {
        StepState.VALIDATING: {"system"},
        StepState.CANCELLED: {"system", "operator"},
        StepState.SUPERSEDED: {"system"},
    },
    StepState.VALIDATING: {
        StepState.WAITING_APPROVAL: {"system"},
        StepState.WAITING_CREDENTIALS: {"system"},
        StepState.READY: {"system"},
        StepState.FAILED: {"system"},
        # §13.1: the lease can be lost during validation — the attempt is
        # superseded, never counted as a dispatch.
        StepState.SUPERSEDED: {"system"},
        StepState.CANCELLED: {"system", "operator"},
    },
    StepState.WAITING_APPROVAL: {
        StepState.READY: {"operator", "system"},
        StepState.CANCELLED: {"operator", "system"},
        StepState.SUPERSEDED: {"system"},
    },
    StepState.WAITING_CREDENTIALS: {
        StepState.READY: {"operator", "system"},
        StepState.CANCELLED: {"operator", "system"},
    },
    StepState.READY: {
        StepState.DISPATCHING: {"system", "agent"},
        StepState.CANCELLED: {"operator", "system"},
        StepState.SUPERSEDED: {"system"},
    },
    StepState.DISPATCHING: {
        StepState.VERIFYING: {"system", "agent"},
        StepState.OUTCOME_UNCERTAIN: {"system"},   # §10.6: lost response
        StepState.FAILED: {"system"},
    },
    StepState.VERIFYING: {
        StepState.VERIFIED: {"verifier", "system"},
        StepState.FAILED: {"verifier", "system"},
        StepState.OUTCOME_UNCERTAIN: {"system"},
        StepState.CANCELLED: {"operator", "system"},   # §10.15 race
    },
    # Terminal / quiescent states
    StepState.VERIFIED: {},
    StepState.FAILED: {},
    StepState.OUTCOME_UNCERTAIN: {},   # reconciliation decides, not a direct retry
    StepState.CANCELLED: {},
    StepState.SUPERSEDED: {},
}


def step_transition_valid(current: StepState, target: StepState,
                          actor: str) -> bool:
    allowed = ALLOWED_STEP_TRANSITIONS.get(current, {})
    return actor in allowed.get(target, set())


class StepTransitionError(Exception):
    def __init__(self, message: str, current: StepState, state_version: int):
        super().__init__(message)
        self.current = current
        self.state_version = state_version


def _row_to_step(row) -> Dict[str, Any]:
    d = dict(row)
    for json_field in ("observation_references_json", "evidence_references_json"):
        try:
            d[json_field.replace("_json", "")] = json.loads(d.pop(json_field, "[]") or "[]")
        except (ValueError, KeyError):
            d[json_field.replace("_json", "")] = []
    return d


class ExecutionStepRepository:
    def __init__(self, db):
        self.db = db

    async def create_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        now = epoch_now()
        step.setdefault("created_at", now)
        step.setdefault("updated_at", now)
        step.setdefault("state_version", 1)
        step.setdefault("state", StepState.OBSERVING.value)
        await self.db.execute(
            """
            INSERT INTO execution_steps
                (step_id, logical_action_id, attempt_id, command_id, run_id,
                 task_id, tenant_id, actor_id, sequence, state, state_version,
                 lease_generation, plan_revision, policy_revision,
                 connector_revision, observation_references_json,
                 proposed_action, validated_action_digest,
                 authorization_reference, budget_reservation_id, effect_id,
                 result_reference, verification_reference, error_code,
                 evidence_references_json, created_at, updated_at, deadline_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (step.get("step_id"), step.get("logical_action_id"),
             step.get("attempt_id"), step.get("command_id"),
             step.get("run_id"), step.get("task_id"), step.get("tenant_id"),
             step.get("actor_id"), step.get("sequence", 0),
             step.get("state"), step.get("state_version"),
             step.get("lease_generation"), step.get("plan_revision"),
             step.get("policy_revision"), step.get("connector_revision"),
             json.dumps(step.get("observation_references") or []),
             step.get("proposed_action"), step.get("validated_action_digest"),
             step.get("authorization_reference"),
             step.get("budget_reservation_id"), step.get("effect_id"),
             step.get("result_reference"), step.get("verification_reference"),
             step.get("error_code"),
             json.dumps(step.get("evidence_references") or []),
             step["created_at"], step["updated_at"], step.get("deadline_at")))
        await self.db.commit()
        return await self.get_step(step["step_id"])

    async def get_step(self, step_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM execution_steps WHERE step_id=?", (step_id,))
        row = await cursor.fetchone()
        return _row_to_step(row) if row else None

    async def steps_for_task(self, run_id: str, task_id: str,
                             limit: int = 100) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM execution_steps WHERE run_id=? AND task_id=? "
            "ORDER BY sequence LIMIT ?", (run_id, task_id, limit))
        return [_row_to_step(r) for r in await cursor.fetchall()]

    async def steps_for_run(self, run_id: str,
                            limit: int = 500) -> List[Dict[str, Any]]:
        """Phase 12: all execution steps for a run — the tool-call evidence
        the evaluation runner aggregates."""
        cursor = await self.db.execute(
            "SELECT * FROM execution_steps WHERE run_id=? "
            "ORDER BY sequence LIMIT ?", (run_id, limit))
        return [_row_to_step(r) for r in await cursor.fetchall()]

    async def transition(self, step_id: str, target: StepState, *,
                         actor: str, expected_state: Optional[StepState] = None,
                         expected_version: Optional[int] = None,
                         evidence_reference: Optional[str] = None,
                         error_code: Optional[str] = None,
                         result_reference: Optional[str] = None,
                         validated_action_digest: Optional[str] = None,
                         policy_revision: Optional[str] = None) -> Dict[str, Any]:
        """§10.6: atomic CAS transition — expected state + version in the
        UPDATE; concurrent cancellation/completion cannot produce
        contradictory terminal states."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state, state_version FROM execution_steps WHERE step_id=?",
                (step_id,))
            row = await cursor.fetchone()
            if not row:
                raise StepTransitionError("Step not found", StepState.FAILED, 0)
            current = StepState(row["state"])
            version = int(row["state_version"])
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
            new_version = version + 1
            sets = "state=?, state_version=?, updated_at=?"
            args: List[Any] = [target.value, new_version, now]
            if evidence_reference:
                sets += ", evidence_references_json=?"
                row2 = await conn.execute(
                    "SELECT evidence_references_json FROM execution_steps WHERE step_id=?",
                    (step_id,))
                fetched = await row2.fetchone()
                existing = json.loads(fetched["evidence_references_json"] or "[]") if fetched else []
                existing.append(evidence_reference)
                args.append(json.dumps(existing))
            if error_code:
                sets += ", error_code=?"
                args.append(error_code)
            if result_reference:
                sets += ", result_reference=?"
                args.append(result_reference)
            # §13.1: the validated-arguments digest is set at VALIDATION
            # time (a proposal is not a dispatch) — evidence at the actual
            # tool boundary.
            if validated_action_digest:
                sets += ", validated_action_digest=?"
                args.append(validated_action_digest)
            # §13.14: the effective prompt version travels on the step.
            if policy_revision:
                sets += ", policy_revision=?"
                args.append(policy_revision)
            args.extend([step_id, version])
            cursor = await conn.execute(
                f"UPDATE execution_steps SET {sets} "
                "WHERE step_id=? AND state_version=?", args)
            if not cursor.rowcount:
                raise StepTransitionError(
                    f"Concurrent step change for {step_id}", current, version)
        return await self.get_step(step_id)

    async def steps_for_logical_action(self, logical_action_id: str) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM execution_steps WHERE logical_action_id=? "
            "ORDER BY sequence", (logical_action_id,))
        return [_row_to_step(r) for r in await cursor.fetchall()]


_repo: Optional[ExecutionStepRepository] = None


def set_execution_step_repo(repo) -> None:
    global _repo
    _repo = repo


def get_execution_step_repo() -> Optional[ExecutionStepRepository]:
    return _repo
