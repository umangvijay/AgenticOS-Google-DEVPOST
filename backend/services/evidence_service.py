"""Execution evidence service (Phase 10 §10.7/§10.8).

Validates that model-proposed acceptance criteria cover the user's requested
outcomes and do not weaken mandatory requirements. Produces structured
verification results with criterion-level detail, evidence references, and
confidence limitations.

§10.8 rules:
- "output is non-empty" is NOT universal completion evidence.
- A result containing "success" or "done" is a claim, not evidence.
- artifact_exists must RESOLVE the artifact, not merely check dict keys.
- A generic click with only interaction-completed evidence is OUTCOME_UNCERTAIN.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.services.outcome_verifier import AcceptanceCriterion, OutcomeVerifier


class Verdict(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
    UNSUPPORTED_VERIFICATION = "UNSUPPORTED_VERIFICATION"


@dataclass
class CriterionResult:
    criterion_kind: str
    passed: bool
    reason: str = ""
    unverifiable: bool = False   # no resolver/context to check


@dataclass
class ExecutionVerificationResult:
    verdict: Verdict
    criterion_results: List[CriterionResult] = field(default_factory=list)
    evidence_references: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)
    confidence_limitations: List[str] = field(default_factory=list)
    next_permitted_action: str = ""


# §10.7: mandatory criteria the model cannot weaken for external-action tasks.
_MANDATORY_FOR_WRITES = ("effect_resolved",)


def validate_proposed_criteria(
    criteria: List[Dict[str, Any]],
    *, task_is_write: bool, requested_outcomes: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """§10.7: the application validates that model-proposed criteria cover
    the user's requested outcomes and do not weaken mandatory requirements.
    Returns (validated_criteria, rejections)."""
    rejections: List[str] = []
    validated = []
    for c in criteria:
        kind = c.get("kind", "")
        if not kind:
            rejections.append("criterion missing 'kind'")
            continue
        # §10.7: a write task MUST have an effect-resolution criterion.
        if task_is_write and kind in ("non_empty", "contains"):
            rejections.append(
                f"criterion '{kind}' is insufficient for a write task — "
                "effect_resolved or artifact_exists required (§10.7)")
            continue
        validated.append(c)
    if task_is_write and not any(
            c.get("kind") in ("effect_resolved", "artifact_exists",
                              "http_ok") for c in validated):
        rejections.append(
            "a write task requires at least one effect-resolution criterion "
            "(§10.7 mandatory requirement)")
    return validated, rejections


def verify_task_outcome(
    *, output: Optional[Dict[str, Any]],
    acceptance_criteria: List[Dict[str, Any]],
    completion_policy: str = "criteria",
    task_is_write: bool = False,
    artifact_resolver: Optional[Callable] = None,
    effect_states: Optional[Dict[str, str]] = None,
    model_claimed_done: bool = False,
) -> ExecutionVerificationResult:
    """§10.7/§10.8: verify the task outcome using the appropriate evidence
    for the completion policy. Returns a structured result with
    criterion-level detail, missing evidence, and confidence limitations."""
    output = output or {}
    effect_states = effect_states or {}
    criterion_results: List[CriterionResult] = []
    evidence_refs: List[str] = []
    missing: List[str] = []
    limitations: List[str] = []

    # For external-effect tasks: check effect ledger states first (§10.18).
    if completion_policy == "external_effect" or task_is_write:
        if not effect_states:
            missing.append("no effect records found for this task")
            limitations.append(
                "no effect ledger records — the action may or may not "
                "have completed; reconcile before retrying (§8.14/§9.8)")
            if model_claimed_done:
                limitations.append(
                    "model claimed done but no effect evidence exists — "
                    "the claim is not evidence (§10.8)")
            return ExecutionVerificationResult(
                verdict=Verdict.OUTCOME_UNCERTAIN,
                missing_evidence=missing,
                confidence_limitations=limitations,
                next_permitted_action="reconcile",
            )
        # §11.0 Finding 2: use the CORRECT effect enum values from
        # EffectState — OUTCOME_UNKNOWN (not OUTCOME_UNCERTAIN), plus
        # PROVIDER_PENDING, PARTIALLY_APPLIED, and DISPATCHING all require
        # explicit handling. CONFIRMED_NOT_APPLIED is a resolved investigation
        # but does NOT satisfy "create the record".
        from backend.models.effect import EffectState as _ES
        uncertain = {eid: s for eid, s in effect_states.items()
                     if s in (_ES.DISPATCHING.value, _ES.OUTCOME_UNKNOWN.value,
                              _ES.PROVIDER_PENDING.value)}
        if uncertain:
            limits = ["pending uncertain external effects — reconcile before "
                      "retrying (§8.14/§9.8)"]
            if model_claimed_done:
                limits.append("model claimed done but uncertain effects exist — "
                              "the claim is not evidence (§10.8)")
            return ExecutionVerificationResult(
                verdict=Verdict.OUTCOME_UNCERTAIN,
                missing_evidence=[f"effect {eid} is {s}" for eid, s in uncertain.items()],
                confidence_limitations=limits,
                next_permitted_action="reconcile",
            )
        # All effects resolved: continue to criteria verification.
        # §11.0 Finding 2: CONFIRMED_NOT_APPLIED is a resolved investigation
        # but does NOT satisfy "create the record" — treat it as FAILED.
        not_applied = {eid: s for eid, s in effect_states.items()
                       if s == _ES.CONFIRMED_NOT_APPLIED.value}
        if not_applied:
            return ExecutionVerificationResult(
                verdict=Verdict.FAILED,
                missing_evidence=[f"effect {eid} confirmed NOT applied" for eid in not_applied],
                confidence_limitations=[
                    "confirmed-not-applied is a resolved investigation but does "
                    "not satisfy the requested operation (§11.0 Finding 2)"],
                next_permitted_action="retry_after_addressing_failure",
            )
        for eid, s in effect_states.items():
            evidence_refs.append(f"effect:{eid}:{s}")

    # Criteria verification via the OutcomeVerifier.
    verifier = OutcomeVerifier()
    criteria_objects = []
    for c in acceptance_criteria:
        kind = c.get("kind", "")
        value = c.get("value")
        # §10.7: effect_resolved is checked by the effect-states logic above,
        # not by the OutcomeVerifier (which doesn't know about the ledger).
        if kind == "effect_resolved":
            criterion_results.append(CriterionResult(
                criterion_kind=kind, passed=True,
                reason="all effects resolved (verified above)"))
            continue
        if kind == "artifact_exists" and artifact_resolver is not None:
            value = {"resolver": artifact_resolver}
        elif kind == "artifact_exists" and isinstance(value, str):
            # No resolver supplied: the criterion is unverifiable (§10.8).
            criteria_objects.append(AcceptanceCriterion(
                kind=kind, value=None,
                description=c.get("description", ""),
            ))
            criterion_results.append(CriterionResult(
                criterion_kind=kind, passed=False,
                reason="UNVERIFIABLE: artifact reference present but no "
                       "resolver supplied (§10.8)",
                unverifiable=True))
            limitations.append("artifact reference not resolved — needs_review")
            continue
        criteria_objects.append(AcceptanceCriterion(
            kind=kind, value=value, description=c.get("description", "")))

    result = verifier.verify(output, criteria_objects or None)

    # Map the verifier result to criterion results.
    for c in criteria_objects:
        ok, reason = c.check(output)
        criterion_results.append(CriterionResult(
            criterion_kind=c.kind, passed=ok, reason=reason,
            unverifiable=reason.startswith("UNVERIFIABLE")))

    has_unverifiable = any(cr.unverifiable for cr in criterion_results)
    # §10.7: when ALL criteria are effect_resolved and the effect states
    # are confirmed, the verifier sees no criteria (needs_review) but the
    # effect_states check above already confirmed them — the combined
    # verdict is VERIFIED. Only non-effect criteria go to the verifier.
    has_passing_effect_criteria = any(
        cr.criterion_kind == "effect_resolved" and cr.passed
        for cr in criterion_results)
    if has_unverifiable:
        verdict = Verdict.NEEDS_REVIEW
        limitations.append("some criteria could not be verified without a "
                           "resolver (§10.8)")
    elif result.verdict == "needs_review" and has_passing_effect_criteria \
            and not any(cr.passed is False for cr in criterion_results):
        # All effect criteria passed; the verifier had no other criteria.
        verdict = Verdict.VERIFIED
    elif result.verdict == "pass":
        verdict = Verdict.VERIFIED
    elif result.verdict == "needs_review":
        verdict = Verdict.NEEDS_REVIEW
    else:
        verdict = Verdict.FAILED

    if model_claimed_done and verdict is not Verdict.VERIFIED:
        limitations.append("model claimed done but verification did not "
                           "confirm — the claim is not evidence (§10.8)")
    if model_claimed_done and verdict is Verdict.NEEDS_REVIEW:
        limitations.append("model claimed done but the criteria could not be "
                           "fully verified — the claim is not evidence (§10.8)")

    next_action = {
        Verdict.VERIFIED: "complete",
        Verdict.FAILED: "retry after addressing the failure or escalate",
        Verdict.NEEDS_REVIEW: "operator review required",
        Verdict.OUTCOME_UNCERTAIN: "reconcile before retrying",
        Verdict.UNSUPPORTED_VERIFICATION: "request human control",
    }.get(verdict, "")

    return ExecutionVerificationResult(
        verdict=verdict,
        criterion_results=criterion_results,
        evidence_references=evidence_refs,
        missing_evidence=missing,
        confidence_limitations=limitations,
        next_permitted_action=next_action,
    )

