"""One authoritative operation-eligibility evaluator (Phase 6 §6.12).

The SAME evaluator answers for backend invocation, the agent tool catalog,
integration detail UI, coverage summaries, activation checks, and self-repair
decisions. It returns a STRUCTURED result — allowed plus machine-readable
reason codes — never a bare boolean.

Enforced checks (§6.12):
1. Caller and tenant access (the caller context is supplied by the caller;
   this evaluator validates connector-owned constraints).
2. Connector enablement.
3. Revision selection and revocation.
4. Exact operation identity (definition digest + credential binding).
5. Current credential authorization.
6. Required validation evidence.
7. Invocation-specific approvals are evaluated by the caller's approval gate
   (this evaluator reports what the policy needs).

Acceptance condition: no single `verified` boolean can make an untested
operation executable, and context-free coverage summaries can never label an
operation currently executable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from backend.models.credential_binding import BindingStatus, CredentialBinding
from backend.services.tool_validation import (
    credential_binding_id_from,
    has_valid_read_probe,
    validation_provenance,
)

EVALUATED_POLICY_VERSION = "p6.v1"

REASON_CONNECTOR_DISABLED = "CONNECTOR_DISABLED"
REASON_REVISION_NOT_ACTIVE = "REVISION_NOT_ACTIVE"
REASON_REVISION_REVOKED = "REVISION_REVOKED"
REASON_UNKNOWN_PROVENANCE = "UNKNOWN_PROVENANCE"
REASON_REVIEW_REQUIRED = "REVIEW_REQUIRED"
REASON_UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
REASON_CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
REASON_CREDENTIAL_CONTEXT_CHANGED = "CREDENTIAL_CONTEXT_CHANGED"
REASON_SCOPE_MISSING = "SCOPE_MISSING"
REASON_VALIDATION_MISSING = "VALIDATION_MISSING"
REASON_VALIDATION_STALE = "VALIDATION_STALE"
REASON_RUNTIME_CHANGED = "RUNTIME_CHANGED"
REASON_WRITE_VALIDATION_REQUIRED = "WRITE_VALIDATION_REQUIRED"


@dataclass
class OperationEligibility:
    allowed: bool
    reason_codes: List[str] = field(default_factory=list)
    revision_id: Optional[str] = None
    operation_digest: Optional[str] = None
    credential_binding_id: Optional[str] = None
    evaluated_policy_version: str = EVALUATED_POLICY_VERSION
    missing_requirements: List[str] = field(default_factory=list)
    stale_evidence_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed, "reason_codes": self.reason_codes,
            "revision_id": self.revision_id,
            "operation_digest": self.operation_digest,
            "credential_binding_id": self.credential_binding_id,
            "evaluated_policy_version": self.evaluated_policy_version,
            "missing_requirements": self.missing_requirements,
            "stale_evidence_ids": self.stale_evidence_ids,
        }


def evaluate_operation_eligibility(
    tool: dict,
    manifest: dict,
    *,
    revision_id: Optional[str] = None,
    revision_revoked: bool = False,
    revision_active: bool = True,
    credential_binding: Optional[CredentialBinding] = None,
    auth_blob=None,
    required_scopes: Optional[List[str]] = None,
    runtime_digest: Optional[str] = None,
    current_runtime_digest: Optional[str] = None,
    write_validation_required: bool = True,
) -> OperationEligibility:
    """Evaluate whether THIS operation can execute NOW (§6.12)."""
    reasons: List[str] = []
    missing: List[str] = []
    stale: List[str] = []

    op = tool.get("operation") or {}
    method = str(op.get("http_method") or "").upper()
    is_write = method not in ("GET", "HEAD", "OPTIONS")

    # 2. Connector enablement.
    if manifest.get("is_enabled") is False:
        reasons.append(REASON_CONNECTOR_DISABLED)

    # 3. Revision selection and revocation.
    if revision_revoked:
        reasons.append(REASON_REVISION_REVOKED)
    elif revision_id is not None and not revision_active:
        reasons.append(REASON_REVISION_NOT_ACTIVE)

    # 4. Exact operation identity: the digest the evidence must match.
    from backend.services.tool_validation import definition_digest
    spec_hash = manifest.get("spec_hash")
    op_digest = definition_digest(tool, spec_hash)

    # 5. Current credential authorization: derive the binding from the auth
    # context; a supplied CredentialBinding must still be current.
    binding_id = credential_binding_id_from(
        auth_blob if auth_blob is not None else manifest.get("auth") or {})
    if credential_binding is not None:
        if credential_binding.status is not BindingStatus.ACTIVE:
            reasons.append(REASON_CREDENTIAL_MISSING)
        elif not credential_binding.is_current_for(binding_id or ""):
            reasons.append(REASON_CREDENTIAL_CONTEXT_CHANGED)
    if manifest.get("auth") and binding_id is None:
        reasons.append(REASON_CREDENTIAL_MISSING)

    # 6. Required validation evidence (context-bound; §6.12 fixes the bypass).
    evidence = (op.get("_validation") or {})
    evidence_binding = evidence.get("credential_binding_id")
    if evidence.get("kind") == "read_probe" and evidence.get("status") == "passed":
        if evidence.get("digest") != op_digest:
            stale.append(f"evidence-digest:{op.get('name') or tool.get('tool_name')}")
        elif evidence_binding is not None and binding_id is not None \
                and evidence_binding != binding_id:
            stale.append(f"evidence-binding:{op.get('name') or tool.get('tool_name')}")
            reasons.append(REASON_VALIDATION_STALE)

    if method in ("GET", "HEAD"):
        if not has_valid_read_probe(tool, spec_hash, binding_id,
                                    require_context=True):
            if evidence.get("status") == "passed" and (
                    evidence.get("digest") != op_digest or
                    (evidence_binding is not None and binding_id is not None
                     and evidence_binding != binding_id)):
                reasons.append(REASON_VALIDATION_STALE)
            else:
                reasons.append(REASON_VALIDATION_MISSING)
                missing.append("current-context read probe")

    # 6b. Provenance: inferred definitions need an explicit review path;
    # unknown provenance cannot silently become executable (§6.4).
    provenance = validation_provenance(tool)
    if provenance == "unknown":
        reasons.append(REASON_UNKNOWN_PROVENANCE)
    elif provenance == "inferred_description":
        reasons.append(REASON_REVIEW_REQUIRED)

    # 6c. Writes require the dedicated write-validation profile (§6.11);
    # the profile itself stays gated until live-write evidence flows through
    # the Phase 5 effect path (§6.2).
    if is_write and write_validation_required:
        reasons.append(REASON_WRITE_VALIDATION_REQUIRED)

    # 7. Scopes: a reduced grant invalidates affected eligibility.
    if required_scopes:
        granted = set(credential_binding.granted_scopes) if credential_binding else set()
        missing_scopes = [s for s in required_scopes if s not in granted]
        if missing_scopes:
            reasons.append(REASON_SCOPE_MISSING)
            missing.extend(f"scope:{s}" for s in missing_scopes)

    # 8. Runtime drift: the adapter/executor identity changed since the
    # evidence was produced.
    if runtime_digest and current_runtime_digest and \
            runtime_digest != current_runtime_digest:
        reasons.append(REASON_RUNTIME_CHANGED)

    allowed = not reasons
    return OperationEligibility(
        allowed=allowed, reason_codes=reasons, revision_id=revision_id,
        operation_digest=op_digest, credential_binding_id=binding_id,
        missing_requirements=missing, stale_evidence_ids=stale,
    )


# ── §12.7: capability readiness (review finding H) ─────────────────────

# The §12.7 readiness ladder, in order. The parent task resumes ONLY at
# CAPABILITY_ACTIVATED — never merely because generation finished.
CAPABILITY_GENERATED = "generated"
CAPABILITY_REGISTERED = "registered"
CAPABILITY_CREDENTIALS_READY = "credentials_ready"
CAPABILITY_OPERATIONS_TESTED = "required_operations_tested"
CAPABILITY_ELIGIBLE = "eligible"
CAPABILITY_ACTIVATED = "activated"
CAPABILITY_NOT_READY = "not_ready"


def evaluate_capability_readiness(mcp_record: Optional[dict],
                                  build: dict,
                                  validation_runs: Optional[List[dict]] = None
                                  ) -> dict:
    """§12.7: separate `generated`, `registered`, `credentials ready`,
    `required operations tested`, `eligible`, and `activated` — and return
    the PRECISE next action when the capability is not ready. A registered
    but disabled or insufficiently tested connector never causes a false
    ready state (§12.7 acceptance)."""
    ladder = [CAPABILITY_GENERATED, CAPABILITY_REGISTERED,
              CAPABILITY_CREDENTIALS_READY, CAPABILITY_OPERATIONS_TESTED,
              CAPABILITY_ELIGIBLE, CAPABILITY_ACTIVATED]
    if build.get("status") != "success":
        return {"ready": False, "state": CAPABILITY_NOT_READY,
                "reached": [], "next_action":
                    f"build is {build.get('status', 'unknown')}",
                "detail": {"build_status": build.get("status")}}
    reached = [CAPABILITY_GENERATED]

    if not mcp_record:
        return {"ready": False, "state": CAPABILITY_GENERATED,
                "reached": reached,
                "next_action": "register the generated candidate",
                "detail": {"mcp_id": build.get("mcp_id")}}
    reached.append(CAPABILITY_REGISTERED)
    if not mcp_record.get("is_enabled", 1):
        return {"ready": False, "state": CAPABILITY_REGISTERED,
                "reached": reached,
                "next_action": "enable the connector (it is registered but disabled)",
                "detail": {"mcp_id": build.get("mcp_id")}}

    cred_ref = build.get("credential_reference")
    if cred_ref:
        # §13.3: the auth context arrives as a parsed dict (`auth`, the
        # SQLite row mapping) or a JSON string (`auth_json`, Firestore).
        auth = mcp_record.get("auth")
        if auth is None:
            auth = mcp_record.get("auth_json") or {}
        if isinstance(auth, str):
            try:
                import json as _json
                auth = _json.loads(auth or "{}")
            except ValueError:
                auth = {}
        if not auth.get("credential_name") and not auth.get("bound"):
            return {"ready": False, "state": CAPABILITY_REGISTERED,
                    "reached": reached,
                    "next_action": (f"connect the account for credential "
                                    f"'{cred_ref}' (needs credentials — the "
                                    "candidate is preserved, no rebuild needed)"),
                    "detail": {"credential_reference": cred_ref}}
    reached.append(CAPABILITY_CREDENTIALS_READY)

    # §13.3: readiness is AUTHORITATIVE — evidence for the EXACT
    # operation, the CURRENT revision, a PASSING result, a live validity
    # window, and a bound credential context. Undeclared requirements are
    # explicit, never silently interpreted as complete coverage.
    if "required_operations" not in build:
        return {"ready": False, "state": CAPABILITY_CREDENTIALS_READY,
                "reached": reached,
                "next_action": ("declare the required operations for this "
                                "capability (coverage cannot be assumed)"),
                "detail": {"required_operations": "undeclared"}}
    required_ops = build.get("required_operations") or []
    # §14.H: UNDECLARED ≠ EMPTY — and a build born from a parked task
    # with an empty declaration has NOT declared coverage either: the
    # originating task's needs were never named, so "no operations
    # required" would let it activate with zero validated operations.
    if not required_ops and build.get("parent_task_id"):
        return {"ready": False, "state": CAPABILITY_CREDENTIALS_READY,
                "reached": reached,
                "next_action": ("declare the operations the originating "
                                "task requires for this capability "
                                "(a task-initiated build cannot declare "
                                "empty coverage)"),
                "detail": {"required_operations": "undeclared_for_task",
                           "parent_task_id": build.get("parent_task_id")}}
    if required_ops:
        now = __import__("backend.models.admission",
                         fromlist=["epoch_now"]).epoch_now()
        validated_ops = set()
        for run in validation_runs or []:
            if run.get("status") != "passed":
                continue   # a failed or superseded run is not evidence
            if mcp_record.get("active_revision_id") and \
                    run.get("revision_id") != mcp_record.get("active_revision_id"):
                continue   # stale revision
            valid_until = run.get("valid_until")
            if valid_until and int(valid_until) <= now:
                continue   # expired evidence
            if cred_ref and not run.get("credential_binding_id"):
                continue   # validated WITHOUT the bound credential context
            validated_ops.add(run.get("operation_id"))
        missing = [op for op in required_ops if op not in validated_ops]
        if missing:
            return {"ready": False,
                    "state": CAPABILITY_CREDENTIALS_READY,
                    "reached": reached,
                    "next_action": ("validate the required operations before "
                                    f"resuming: {', '.join(sorted(missing))}"),
                    "detail": {"missing_operations": sorted(missing)}}
    reached.append(CAPABILITY_OPERATIONS_TESTED)
    reached.append(CAPABILITY_ELIGIBLE)

    if not mcp_record.get("active_revision_id"):
        return {"ready": False, "state": CAPABILITY_ELIGIBLE,
                "reached": reached,
                "next_action": "activate the eligible revision",
                "detail": {"mcp_id": build.get("mcp_id")}}
    reached.append(CAPABILITY_ACTIVATED)
    return {"ready": True, "state": CAPABILITY_ACTIVATED,
            "reached": ladder, "next_action": None,
            "detail": {"mcp_id": build.get("mcp_id"),
                       "revision_id": mcp_record.get("active_revision_id")}}
