"""Self-repair through immutable candidate releases (Phase 7 §7.22).

Repair sequence: classify failure → gather sanitized evidence → decide
whether regeneration is justified → create a CANDIDATE artifact (never
modify a running artifact in place) → validate through the ordinary gates →
activate through the ordinary deployment service.

The repair proposer has LIMITED AUTHORITY (§7.22): it cannot weaken its
acceptance suite, approve itself, alter the sandbox policy, or publish
directly — every candidate goes through the same validation + attestation +
deployment gates as any other artifact.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 3
MAX_REPAIR_COST_USD = 2.0


class FailureCategory:
    """§7.22 table: failure classification → the appropriate response."""
    SDK_INCOMPATIBILITY = "sdk_incompatibility"
    CREDENTIAL_FAILURE = "credential_failure"
    RATE_LIMITING = "rate_limiting"
    SCHEMA_MISMATCH = "schema_mismatch"
    ISOLATION_DENIAL = "isolation_denial"
    UNCERTAIN_WRITE = "uncertain_write"
    PROVIDER_OUTAGE = "provider_outage"
    ADAPTER_DEFECT = "adapter_defect"


# §7.22: the classified response per category. Repairs are justified ONLY
# where the definition or adapter is actually the problem.
RESPONSES: Dict[str, Dict[str, str]] = {
    FailureCategory.SDK_INCOMPATIBILITY: {
        "action": "regenerate_candidate",
        "note": "Update the candidate template or dependency set and retest"},
    FailureCategory.CREDENTIAL_FAILURE: {
        "action": "request_credential_resolution",
        "note": "Request credential resolution; avoid unnecessary regeneration"},
    FailureCategory.RATE_LIMITING: {
        "action": "backoff_within_budget",
        "note": "Back off within budget; no regeneration"},
    FailureCategory.SCHEMA_MISMATCH: {
        "action": "regenerate_candidate",
        "note": "Investigate definition drift and create a revised candidate"},
    FailureCategory.ISOLATION_DENIAL: {
        "action": "explain_missing_capability",
        "note": "Explain the missing capability; do not automatically broaden access"},
    FailureCategory.UNCERTAIN_WRITE: {
        "action": "reconcile_original_effect",
        "note": "Reconcile the original effect (Phase 5) before any retry"},
    FailureCategory.PROVIDER_OUTAGE: {
        "action": "backoff_within_budget",
        "note": "Provider-side; no regeneration"},
    FailureCategory.ADAPTER_DEFECT: {
        "action": "regenerate_candidate",
        "note": "Adapter defect: produce a candidate patch"},
}


@dataclass
class RepairProposal:
    proposal_id: str
    failure_category: str
    justified: bool
    action: str
    note: str
    candidate_artifact_id: Optional[str] = None
    source_diff_summary: Optional[str] = None
    permission_diff_summary: Optional[str] = None
    blocked_reason: Optional[str] = None
    attempts_used: int = 0


def classify_failure(error: str, *, status_code: Optional[int] = None,
                     protocol_error: Optional[str] = None) -> str:
    """§7.22 step 1: classify from sanitized evidence (no provider bodies)."""
    lowered = (error or "").lower()
    if status_code == 401 or status_code == 403 or "unauthorized" in lowered:
        return FailureCategory.CREDENTIAL_FAILURE
    if status_code == 429 or "rate limit" in lowered:
        return FailureCategory.RATE_LIMITING
    if status_code and status_code >= 500 or "provider outage" in lowered:
        return FailureCategory.PROVIDER_OUTAGE
    if "schema" in lowered or protocol_error == "schema_mismatch":
        return FailureCategory.SCHEMA_MISMATCH
    if "isolation" in lowered or "sandbox" in lowered:
        return FailureCategory.ISOLATION_DENIAL
    if "uncertain" in lowered:
        return FailureCategory.UNCERTAIN_WRITE
    return FailureCategory.ADAPTER_DEFECT


def propose_repair(failure_category: str, *, attempts_used: int = 0,
                   estimated_cost_usd: float = 0.0) -> RepairProposal:
    """§7.22 steps 3-5: decide whether regeneration is justified and create
    the proposal. The proposal is NOT execution authority — the candidate
    must pass the ordinary validation/attestation/deployment gates."""
    response = RESPONSES.get(failure_category, RESPONSES[FailureCategory.ADAPTER_DEFECT])
    justified = response["action"] == "regenerate_candidate"
    blocked = None
    if attempts_used >= MAX_REPAIR_ATTEMPTS:
        justified = False
        blocked = f"repair attempt limit reached ({MAX_REPAIR_ATTEMPTS})"
    elif estimated_cost_usd >= MAX_REPAIR_COST_USD:
        justified = False
        blocked = "repair cost budget reached"
    return RepairProposal(
        proposal_id=f"rp_{failure_category}_{attempts_used + 1}",
        failure_category=failure_category, justified=justified,
        action=response["action"], note=response["note"],
        blocked_reason=blocked, attempts_used=attempts_used)


def repair_candidate_created(proposal: RepairProposal, *,
                             candidate_artifact_id: str,
                             source_diff: str, permission_diff: str) -> RepairProposal:
    """§7.22 steps 5-6: record the candidate and the human-readable source +
    permission differences. The candidate proceeds through static checks →
    protocol/isolation tests → ordinary deployment evaluation (§7.22 steps
    7-10); the proposer cannot approve itself."""
    proposal.candidate_artifact_id = candidate_artifact_id
    proposal.source_diff_summary = source_diff
    proposal.permission_diff_summary = permission_diff
    return proposal
