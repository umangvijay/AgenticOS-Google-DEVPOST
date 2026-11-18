"""Bounded deliverable self-healing (Phase 17 §17.18) — reuses the
recovery episode system. Permitted repairs are TYPED; every repair
carries the original failure evidence, the allowed change scope, the
attempt budget, and requires independent verification. A repair must
not invent facts, weaken acceptance criteria, broaden access, or
silently publish a new external effect."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

MAX_REPAIR_ATTEMPTS = 3


class RepairKind:
    """§17.18: the permitted repair types — each names the scope of
    change it is allowed to make."""
    RETRY_READ_UPLOAD = "retry_read_upload"        # safe retry, no content change
    RE_RENDER = "re_render"                        # same source revision, new render
    REPAIR_LAYOUT = "repair_layout"                # formatting only
    REGENERATE_MISSING_FILE = "regenerate_missing_file"  # against the approved manifest
    REQUEST_MISSING_SOURCE = "request_missing_source"    # clarification, not invention


PERMITTED_REPAIRS = {
    RepairKind.RETRY_READ_UPLOAD: "no content change; the same bytes re-upload",
    RepairKind.RE_RENDER: "same source revision re-rendered (new revision)",
    RepairKind.REPAIR_LAYOUT: "formatting only — factual content untouched",
    RepairKind.REGENERATE_MISSING_FILE: "the missing file only, against the approved manifest",
    RepairKind.REQUEST_MISSING_SOURCE: "no content change — the user is asked",
}


@dataclass
class DeliverableRepair:
    repair_id: str
    deliverable_id: str
    repair_kind: str
    failure_evidence: Dict[str, Any]     # the ORIGINAL failure
    allowed_scope: str
    attempts_remaining: int = MAX_REPAIR_ATTEMPTS
    budget_units_remaining: int = 100
    new_revision: Optional[int] = None
    verification_required: bool = True
    state: str = "proposed"              # proposed | executed | verified | exhausted | escalated
    created_at: int = field(default_factory=epoch_now)


def classify_deliverable_failure(*, verdict: Optional[str] = None,
                                 blocking_reasons: Optional[List[str]] = None,
                                 stored_bytes_missing: bool = False,
                                 transport_error: bool = False,
                                 ) -> Optional[str]:
    """The failure → permitted repair mapping. Missing SOURCE facts
    escalate to clarification — never invention; verification failures
    from factual claims are NOT repairable automatically."""
    reasons = " ".join(blocking_reasons or []).lower()
    if stored_bytes_missing or transport_error:
        return RepairKind.RETRY_READ_UPLOAD
    if "layout" in reasons or "render" in reasons:
        return RepairKind.RE_RENDER
    if "corrupt" in reasons or "pdf" in reasons:
        return RepairKind.RE_RENDER
    if "missing file" in reasons:
        return RepairKind.REGENERATE_MISSING_FILE
    if "missing" in reasons and ("date" in reasons or "employer" in reasons
                                 or "fact" in reasons):
        # §17.18 acceptance: a missing employment date triggers
        # CLARIFICATION, never invention.
        return RepairKind.REQUEST_MISSING_SOURCE
    return None   # not automatically repairable — escalate


def propose_repair(*, deliverable_id: str, repair_kind: str,
                   failure_evidence: Dict[str, Any],
                   attempts_used: int = 0,
                   budget_units_used: int = 0) -> DeliverableRepair:
    if repair_kind not in PERMITTED_REPAIRS:
        raise ValueError(
            f"repair kind '{repair_kind}' is not permitted (§17.18)")
    repair = DeliverableRepair(
        repair_id=f"drp_{epoch_now()}", deliverable_id=deliverable_id,
        repair_kind=repair_kind, failure_evidence=failure_evidence,
        allowed_scope=PERMITTED_REPAIRS[repair_kind],
        attempts_remaining=max(0, MAX_REPAIR_ATTEMPTS - attempts_used),
        budget_units_remaining=max(0, 100 - budget_units_used))
    if repair.attempts_remaining <= 0:
        repair.state = "exhausted"
    return repair


def repair_may_change_content(repair: DeliverableRepair) -> bool:
    """Content-changing repairs create a NEW REVISION and REQUIRE
    independent verification; content-preserving repairs do not."""
    return repair.repair_kind in (RepairKind.RE_RENDER,
                                  RepairKind.REGENERATE_MISSING_FILE)


def exhaust_or_escalate(repair: DeliverableRepair,
                        *, verified: bool) -> str:
    """The exhaustion/escalation contract: a repair that cannot verify
    consumes an attempt; at zero, it ESCALATES — never silently claims
    success."""
    if verified:
        repair.state = "verified"
        return repair.state
    repair.attempts_remaining = max(0, repair.attempts_remaining - 1)
    if repair.attempts_remaining <= 0:
        repair.state = "escalated"
        repair.verification_required = True
    else:
        repair.state = "proposed"
    return repair.state
