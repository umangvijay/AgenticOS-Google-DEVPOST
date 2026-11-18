"""Standalone MCP deployment service (Phase 7 §7.20).

Rechecks EVERYTHING from current records at deployment time:
caller authorization, artifact integrity, trusted attestation coverage,
operation-level Phase 6 eligibility, revocation, and budget. Selects images
by immutable digest; changing any execution-relevant artifact content after
testing blocks deployment until relevant validation is repeated.

Avoids: test image A → rebuild → deploy image B. The flow is build immutable
→ test that digest → deploy that digest.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.mcp_artifact import (
    DeploymentStatus,
    IsolationValidationStatus,
    ProtocolValidationStatus,
)
from backend.services.artifact_attestation_service import attestation_covers
from backend.services.mcp_protocol_validator import suite_digest, REQUIRED_PROTOCOL_CASES

logger = logging.getLogger(__name__)


def evaluate_deployment_eligibility(
    *,
    artifact: Dict[str, Any],
    attestations: List[Dict[str, Any]],
    owner_user_id: str,
    caller_user_id: str,
    is_admin: bool = False,
    connector_enabled: bool = True,
    connector_revoked: bool = False,
    operation_eligibility: Optional[List[Dict[str, Any]]] = None,
    isolation_profile_verified: bool = False,
    budget_available: bool = True,
    content_digest_now: Optional[str] = None,
    expected_runner_profile_digest: Optional[str] = None,
) -> Dict[str, Any]:
    """§7.20: evaluate deployment from CURRENT records. Returns allowed +
    blocked reasons; the deployment service refuses on any failure."""
    reasons: List[str] = []

    # 1. Caller authorization + 2. ownership.
    if owner_user_id != caller_user_id and not is_admin:
        reasons.append("CALLER_NOT_AUTHORIZED")

    # 3. Artifact byte integrity: current content must match the recorded
    # content digest — any post-test change blocks deployment.
    if not content_digest_now:
        reasons.append("ARTIFACT_INTEGRITY_NOT_VERIFIED")
    elif content_digest_now != artifact.get("content_digest"):
        reasons.append("ARTIFACT_CONTENT_CHANGED")

    # 4-6. Trusted validation evidence covering this artifact + runtime + suite.
    artifact_digest = artifact.get("content_digest")
    runtime_digest = artifact.get("runtime_image_digest")
    covered = any(
        attestation_covers(a, artifact_digest=artifact_digest,
                           runtime_image_digest=runtime_digest or "",
                           test_suite_digest=suite_digest())
        and bool(expected_runner_profile_digest)
        and a.get("runner_profile_digest") == expected_runner_profile_digest
        and isinstance(a.get("observed_results"), dict)
        and all(a["observed_results"].get(case["case"]) == "passed"
                for case in REQUIRED_PROTOCOL_CASES)
        for a in attestations if isinstance(a, dict))
    if artifact.get("protocol_validation_status") != ProtocolValidationStatus.PASSED.value:
        reasons.append("PROTOCOL_VALIDATION_NOT_PASSED")
    if not covered:
        reasons.append("NO_TRUSTED_ATTESTATION_FOR_THIS_ARTIFACT")

    # 7. Isolation profile.
    if isolation_profile_verified is not True or artifact.get("isolation_validation_status") != \
            IsolationValidationStatus.PASSED.value:
        # §7.5: isolation is required for managed execution — protocol
        # passes cannot bypass it.
        reasons.append("ISOLATION_PROFILE_NOT_VERIFIED")

    # 9. Operation-level Phase 6 eligibility (every op allowed).
    if not operation_eligibility:
        reasons.append("OPERATION_ELIGIBILITY_NOT_VERIFIED")
    blocked_ops = [op.get("tool_name") for op in (operation_eligibility or [])
                   if op.get("allowed") is not True]
    if blocked_ops:
        reasons.append("OPERATIONS_NOT_ELIGIBLE: " + ", ".join(blocked_ops))

    # 10-11. Connector + revocation state.
    if not connector_enabled:
        reasons.append("CONNECTOR_DISABLED")
    if connector_revoked:
        reasons.append("CONNECTOR_REVOKED")

    # 12. Budget.
    if not budget_available:
        reasons.append("BUDGET_UNAVAILABLE")

    allowed = not reasons
    status = (DeploymentStatus.ELIGIBLE.value if allowed
              else DeploymentStatus.BLOCKED.value)
    return {"allowed": allowed, "status": status, "blocked_reasons": reasons,
            "artifact_digest": artifact_digest,
            "runtime_image_digest": runtime_digest}
