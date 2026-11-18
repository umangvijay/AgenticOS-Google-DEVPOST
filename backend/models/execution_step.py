"""ReAct execution step records (Phase 10).

Structured observation → action → policy decision → result → verification
records with IDs, timestamps, and evidence references (§10.1). Every step
is durable, bounded, and outcome-checked: model-declared completion can
REQUEST verification but cannot alone establish external success (§10.4).

Untrusted page/tool content is structurally separate from policy and user
instructions (§10.2): the `untrusted_input` field carries the observation,
never the decision.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


def new_step_id() -> str:
    return f"step_{uuid.uuid4().hex[:24]}"


class StepKind(str, Enum):
    OBSERVATION = "observation"
    ACTION_PROPOSAL = "action_proposal"
    POLICY_DECISION = "policy_decision"
    EXECUTION = "execution"
    OUTCOME_VERIFICATION = "outcome_verification"
    CHECKPOINT = "checkpoint"


class StepVerdict(str, Enum):
    PROPOSED = "proposed"
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    EXECUTED = "executed"
    FAILED = "failed"
    VERIFIED = "verified"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


@dataclass
class ExecutionStep:
    """§10.1 — one ReAct cycle record. `observation` carries UNTRUSTED
    content (page text, tool output); the model's decision is structurally
    separate. Policy decisions reference the criteria evaluated, not the
    model's reasoning."""
    step_id: str = field(default_factory=new_step_id)
    run_id: str = ""
    task_id: str = ""
    step_index: int = 0
    kind: StepKind = StepKind.OBSERVATION
    verdict: StepVerdict = StepVerdict.PROPOSED
    # What was observed (untrusted — redacted before model input, §9.18)
    observation_summary: str = ""
    observation_ref: Optional[str] = None
    # What the model proposed
    proposed_action: Optional[str] = None
    proposed_arguments: Dict[str, Any] = field(default_factory=dict)
    # What policy decided and why
    policy_decision: Optional[str] = None        # allowed | blocked | approval_required
    policy_reasons: List[str] = field(default_factory=list)
    approval_reference: Optional[str] = None
    # What actually executed and the observed outcome
    execution_tool: Optional[str] = None
    execution_outcome: Optional[str] = None      # outcome_verifier verdict
    evidence_references: List[str] = field(default_factory=list)
    # Bounds and timestamps
    started_at: int = 0
    completed_at: int = 0
    duration_ms: int = 0
    attempt: int = 1
    # Integrity
    content_digest: str = ""                     # §10.1 evidence binding

    def finalize(self) -> "ExecutionStep":
        import hashlib
        material = json.dumps({
            "step_id": self.step_id, "kind": self.kind.value,
            "verdict": self.verdict.value,
            "action": self.proposed_action,
            "tool": self.execution_tool,
            "outcome": self.execution_outcome,
            "evidence": sorted(self.evidence_references),
        }, sort_keys=True, default=str)
        self.content_digest = hashlib.sha256(material.encode()).hexdigest()[:32]
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id, "run_id": self.run_id,
            "task_id": self.task_id, "step_index": self.step_index,
            "kind": self.kind.value, "verdict": self.verdict.value,
            "observation_summary": self.observation_summary[:300],
            "proposed_action": self.proposed_action,
            "policy_decision": self.policy_decision,
            "policy_reasons": self.policy_reasons,
            "approval_reference": self.approval_reference,
            "execution_tool": self.execution_tool,
            "execution_outcome": self.execution_outcome,
            "evidence_references": self.evidence_references,
            "duration_ms": self.duration_ms, "attempt": self.attempt,
            "content_digest": self.content_digest,
        }


# ── §10.3: the bounded-loop enforcement ───────────────────────────────

@dataclass
class ReActBounds:
    """§10.3 — iteration, wall-time, spend, tool-call, and repeated-action
    caps for ONE task's reasoning loop. Enforced OUTSIDE the model."""
    max_iterations: int = 12
    max_wall_time_seconds: int = 300
    max_spend_units: int = 100_000            # Phase 2 tokens
    max_tool_calls: int = 20
    max_repeated_equivalent_actions: int = 2  # same action + same state

    def __post_init__(self) -> None:
        if self.max_iterations <= 0 or self.max_tool_calls <= 0:
            raise ValueError("ReAct bounds must be positive")

    def check(self, *, iterations: int, elapsed_seconds: float,
              spend_units: int, tool_calls: int,
              repeated_actions: int) -> List[str]:
        """Returns the list of exceeded bounds (empty = within bounds)."""
        exceeded = []
        if iterations >= self.max_iterations:
            exceeded.append(f"iterations: {iterations}/{self.max_iterations}")
        if elapsed_seconds >= self.max_wall_time_seconds:
            exceeded.append(f"wall time: {elapsed_seconds:.0f}s/"
                            f"{self.max_wall_time_seconds}s")
        if spend_units >= self.max_spend_units:
            exceeded.append(f"spend: {spend_units}/{self.max_spend_units}")
        if tool_calls >= self.max_tool_calls:
            exceeded.append(f"tool calls: {tool_calls}/{self.max_tool_calls}")
        if repeated_actions >= self.max_repeated_equivalent_actions:
            exceeded.append(f"repeated equivalent actions: "
                            f"{repeated_actions}/{self.max_repeated_equivalent_actions}")
        return exceeded


# ── §10.4: outcome-gated completion ───────────────────────────────────

class CompletionDecision(str, Enum):
    COMPLETE = "complete"                      # evidence supports the goal
    CONTINUE = "continue"                      # more work needed
    BLOCKED = "blocked"                        # policy denial / budget exhausted
    UNCERTAIN = "uncertain"                    # needs reconciliation
    NEEDS_VERIFICATION = "needs_verification"  # model claimed done; verify first


def decide_completion(*, model_claimed_done: bool,
                      verification_verdict: Optional[str],
                      bounds_exceeded: List[str],
                      pending_uncertain_effects: int = 0) -> CompletionDecision:
    """§10.4: model-declared completion can REQUEST verification but cannot
    alone establish external success. The verifier's verdict (or a pending
    uncertain effect) decides."""
    if pending_uncertain_effects > 0:
        return CompletionDecision.UNCERTAIN
    if bounds_exceeded:
        return CompletionDecision.BLOCKED
    if not model_claimed_done:
        return CompletionDecision.CONTINUE
    # The model says done — verification must agree.
    if verification_verdict == "pass":
        return CompletionDecision.COMPLETE
    if verification_verdict == "needs_review":
        return CompletionDecision.NEEDS_VERIFICATION
    if verification_verdict == "fail":
        return CompletionDecision.CONTINUE   # keep working, or fail at bounds
    return CompletionDecision.NEEDS_VERIFICATION
