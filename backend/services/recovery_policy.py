"""Formal self-healing recovery policy (audit §3, v5.6 review §3).

Recovery is a controlled loop: **detect → classify → select an allowed
recovery → execute within limits → verify the outcome → record evidence →
escalate when unresolved.** This module is the ALLOWED-SELECTION gate:
deterministic, never model judgement, and bounded — a recovery must not
silently broaden permissions, change the user's goal, or loop forever.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.engine.failure_classifier import FailureClassification


@dataclass
class RecoveryAttempt:
    """One recorded recovery attempt: what was classified, what was tried,
    with which inputs — the repeated-action detector hashes the ACTION."""
    attempt: int
    category: str
    action: str                     # retry | repair_arguments | refresh_credential | abort
    action_hash: str                # hash of (tool, category, normalized changed args)
    rationale: str
    verified: bool = False          # postcondition held after execution?
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class RecoveryDecision:
    allowed: bool
    reason: str
    max_attempts: int
    deadline: str                   # absolute UTC cutoff for this recovery
    escalate: bool = False          # unresolved → create an incident


class RecoveryPolicy:
    """Deterministic gate in front of every recovery. category → max attempts
    comes from the failure classifier's retry budget; repeated identical
    actions are refused even inside the budget."""

    # Category → overall recovery ceiling (stricter than the classifier's
    # per-call auto-retry budget: RECOVERY retries re-plan, they cost more).
    CATEGORY_CEILINGS = {
        "transient_network": 3,
        "timeout": 3,
        "server_error": 2,
        "rate_limited": 2,
        "schema_mismatch": 2,       # repair arguments — but only twice
        "auth_expired": 2,          # refresh credential — but only twice
        "auth_invalid": 0,          # credential was rejected: human required
        "not_found": 1,
        "internal": 0,
    }

    DEADLINE_SECONDS = 900          # a recovery may run at most 15 minutes
    MAX_DEADLINE_MISSES_BEFORE_ESCALATION = 1

    def __init__(self, attempts: Optional[List[RecoveryAttempt]] = None,
                 recovery_started_at: Optional[str] = None):
        self.attempts = attempts or []
        self.recovery_started_at = recovery_started_at

    @staticmethod
    def action_hash(tool_name: str, category: str, changed_arguments: Dict[str, Any]) -> str:
        material = {"tool": tool_name, "category": category,
                    "args": changed_arguments or {}}
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()

    def decide(self, classification: FailureClassification, tool_name: str,
               proposed_changed_arguments: Optional[Dict[str, Any]] = None,
               action_hash: Optional[str] = None) -> RecoveryDecision:
        """Is a recovery with THIS classification and THESE changed arguments
        allowed right now? Deterministic on the attempt ledger."""
        ceiling = self.CATEGORY_CEILINGS.get(classification.category, 0)
        category_attempts = sum(1 for a in self.attempts if a.category == classification.category)

        if classification.category in ("auth_invalid", "internal") or ceiling == 0:
            return RecoveryDecision(
                False, f"Category '{classification.category}' is not auto-recoverable: "
                       f"{classification.recovery_hint}", 0, self._deadline(), escalate=True)

        if category_attempts >= ceiling:
            return RecoveryDecision(
                False, f"Recovery ceiling reached for '{classification.category}' "
                       f"({category_attempts}/{ceiling}). Escalating with evidence.",
                ceiling, self._deadline(), escalate=True)

        # Repeated-action detector: the same (tool, category, arguments)
        # recovery tried twice proves the loop is ineffective — stop.
        proposed_hash = self.action_hash(tool_name, classification.category,
                                         proposed_changed_arguments or {})
        same_action = sum(1 for a in self.attempts if a.action_hash == proposed_hash)
        if same_action >= 1:
            return RecoveryDecision(
                False, "Identical recovery already attempted with the same "
                       "arguments — repeating it would be a blind retry. "
                       "Escalating with the recorded evidence.",
                ceiling, self._deadline(), escalate=True)

        if self._deadline_exceeded():
            return RecoveryDecision(
                False, "Recovery deadline exceeded; escalating instead of "
                       "continuing to spend on repairs.",
                ceiling, self._deadline(), escalate=True)

        return RecoveryDecision(True, "Recovery allowed within policy",
                                ceiling, self._deadline())

    @staticmethod
    def verify_outcome(result: Optional[Dict[str, Any]]) -> bool:
        """Postcondition check: a recovery only 'worked' if the retry produced
        usable output — not merely the absence of an exception."""
        if not isinstance(result, dict):
            return False
        for key in ("reply", "message", "data", "result", "output", "tool_result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (dict, list)) and value:
                return True
        if result.get("status_code") and 200 <= int(result["status_code"]) < 300:
            return True
        return False

    def _deadline(self) -> str:
        base = self.recovery_started_at or datetime.now(timezone.utc).isoformat()
        start = datetime.fromisoformat(base.replace("Z", "+00:00"))
        return (start + timedelta(seconds=self.DEADLINE_SECONDS)).isoformat()

    def _deadline_exceeded(self) -> bool:
        if not self.recovery_started_at:
            return False
        start = datetime.fromisoformat(self.recovery_started_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= start + timedelta(seconds=self.DEADLINE_SECONDS)

    @staticmethod
    def incident(task_id: str, run_id: str, attempts: List[RecoveryAttempt],
                 classification: FailureClassification, reason: str) -> Dict[str, Any]:
        """Escalation payload: everything an operator needs, nothing more."""
        return {
            "kind": "recovery_incident",
            "task_id": task_id,
            "run_id": run_id,
            "category": classification.category,
            "reason": reason,
            "recovery_hint": classification.recovery_hint,
            "attempts": [{"attempt": a.attempt, "category": a.category, "action": a.action,
                          "rationale": a.rationale, "verified": a.verified, "at": a.at}
                         for a in attempts],
            "user_options": [
                "Fix the inputs and retry the task from the run timeline",
                "Attach or refresh the credential in Vault, then retry",
                "Cancel the run",
            ],
        }
