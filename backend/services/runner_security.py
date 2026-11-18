"""Isolation acceptance profile + runtime decision record (Phase 7 §7.5).

The runner CONTRACT was defined in isolated_runner.py. This module defines
the ISOLATION ACCEPTANCE TESTS a backend must pass before managed execution
is enabled, the runtime DECISION record, and the capability model for
operation-broker access (§7.14/§7.15).

Decision (§7.5): Cloud Run sandbox is the selected FIRST candidate for the
Google Cloud deployment — with the documented constraint that its default
filesystem allows reading the parent container's root filesystem, so a
minimal dedicated runner image is REQUIRED and no application secrets or
customer files may exist in it. Managed execution remains UNAVAILABLE until
the backend is proven against ISOLATION_ACCEPTANCE_TESTS in a live
deployment; the tests below are the executable definition of "proven".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class IsolationControl(str, Enum):
    """Each control is a REQUIRED property of the runtime (§7.5/§7.12/§7.13/
    §7.14). A backend that cannot enforce one records it as unsupported and
    managed execution stays unavailable."""
    FILESYSTEM_BUNDLE_ONLY = "filesystem_bundle_only"       # §7.13: no parent-dir reads
    WORKSPACE_SEPARATION = "workspace_separation"           # writable workspace separate
    NETWORK_DEFAULT_DENY = "network_default_deny"           # §7.14
    NETWORK_BROKER_ONLY = "network_broker_only"             # §7.14/§7.15: writes via broker
    METADATA_BLOCKED = "metadata_blocked"                   # cloud metadata unreachable
    SUPERVISOR_DEADLINE = "supervisor_deadline"             # §7.12 wall time
    MEMORY_LIMIT = "memory_limit"
    PROCESS_LIMIT = "process_limit"
    OUTPUT_CAPPED = "output_capped"
    CANCEL_TERMINATION = "cancel_termination"               # §7.18: non-cooperative stop
    DESCENDANT_CLEANUP = "descendant_cleanup"               # §7.18: no surviving children
    TENANT_ISOLATION = "tenant_isolation"                   # §7.13: no cross-tenant reads


@dataclass
class IsolationAcceptanceTest:
    """One executable acceptance test: a hostile fixture and the observable
    result that proves the control. Run ONLY inside the designated sandbox
    harness (§7.23)."""
    control: IsolationControl
    name: str
    fixture: str          # identifier of the adversarial fixture to run
    expect: str           # observable result proving enforcement
    description: str


ISOLATION_ACCEPTANCE_TESTS: List[IsolationAcceptanceTest] = [
    IsolationAcceptanceTest(
        IsolationControl.FILESYSTEM_BUNDLE_ONLY, "seeded_host_secret_unreadable",
        "read_seeded_host_secret", "denied",
        "A seeded host secret outside the input bundle is unreadable (§7.13)."),
    IsolationAcceptanceTest(
        IsolationControl.TENANT_ISOLATION, "cross_tenant_input_denied",
        "read_other_tenant_input", "denied",
        "One tenant cannot read another tenant's workspace (§7.13)."),
    IsolationAcceptanceTest(
        IsolationControl.METADATA_BLOCKED, "cloud_metadata_unreachable",
        "access_cloud_metadata", "denied",
        "Cloud metadata endpoints are unreachable (§7.14)."),
    IsolationAcceptanceTest(
        IsolationControl.NETWORK_DEFAULT_DENY, "unapproved_destination_denied",
        "connect_unapproved_host", "denied",
        "Connections to unapproved destinations are denied (§7.14)."),
    IsolationAcceptanceTest(
        IsolationControl.NETWORK_DEFAULT_DENY, "raw_socket_bypass_denied",
        "direct_socket_connect", "denied",
        "Direct socket access bypassing the HTTP library is still denied (§7.14)."),
    IsolationAcceptanceTest(
        IsolationControl.MEMORY_LIMIT, "memory_exhaustion_terminated",
        "exhaust_memory", "terminated_within_boundary",
        "Exhausting memory terminates the workload within its boundary (§7.12)."),
    IsolationAcceptanceTest(
        IsolationControl.PROCESS_LIMIT, "process_spawn_bounded",
        "spawn_excessive_processes", "limit_enforced",
        "Excessive process spawning is bounded (§7.12)."),
    IsolationAcceptanceTest(
        IsolationControl.OUTPUT_CAPPED, "unbounded_output_capped",
        "emit_unbounded_output", "capped",
        "Unbounded output is capped and handled predictably (§7.12)."),
    IsolationAcceptanceTest(
        IsolationControl.CANCEL_TERMINATION, "noncooperative_cancel_terminated",
        "ignore_cancellation", "supervisor_terminated",
        "A non-cooperative server is terminated by the supervisor (§7.18)."),
    IsolationAcceptanceTest(
        IsolationControl.DESCENDANT_CLEANUP, "descendants_do_not_survive",
        "spawn_descendant_then_exit", "cleaned",
        "Descendants do not survive cancellation or exit (§7.18)."),
]


@dataclass
class RuntimeDecision:
    """§7.5 decision record — persisted documentation of the evaluation."""
    backend: str
    status: str                       # selected | evaluated_not_selected | pending_proof
    controls_supported: List[str] = field(default_factory=list)
    controls_unsupported: List[str] = field(default_factory=list)
    notes: str = ""
    proven: bool = False              # True only after the acceptance tests pass live


# §7.5 decision: Cloud Run sandbox selected as the first candidate; NOT yet
# proven (managed execution stays unavailable until the acceptance tests
# pass in the deployment).
RUNTIME_DECISIONS: List[RuntimeDecision] = [
    RuntimeDecision(
        backend="cloud_run_sandbox",
        status="selected_pending_proof",
        controls_supported=[
            IsolationControl.NETWORK_DEFAULT_DENY.value,   # blocks outbound by default
            IsolationControl.METADATA_BLOCKED.value,       # separates metadata access
            IsolationControl.SUPERVISOR_DEADLINE.value,
            IsolationControl.MEMORY_LIMIT.value,           # shared host allocation — capacity planned
            IsolationControl.OUTPUT_CAPPED.value,
        ],
        controls_unsupported=[
            # §7.5 documented constraint: default filesystem allows reading
            # the parent container's root filesystem — requires a MINIMAL
            # dedicated runner image with no secrets/customer files.
            IsolationControl.FILESYSTEM_BUNDLE_ONLY.value,
        ],
        notes=(
            "Selected first candidate for the Google Cloud deployment. "
            "Constraint: the sandbox can read the parent container's root "
            "filesystem, so a minimal dedicated runner image is REQUIRED and "
            "must contain no application secrets or customer files. CPU/"
            "memory are shared with the host container allocation — capacity "
            "must be planned (§7.5)."),
    ),
    RuntimeDecision(
        backend="gvisor_runner",
        status="evaluated_not_selected",
        controls_supported=[
            IsolationControl.FILESYSTEM_BUNDLE_ONLY.value,
            IsolationControl.NETWORK_DEFAULT_DENY.value,
            IsolationControl.METADATA_BLOCKED.value,
            IsolationControl.SUPERVISOR_DEADLINE.value,
            IsolationControl.MEMORY_LIMIT.value,
            IsolationControl.PROCESS_LIMIT.value,
            IsolationControl.OUTPUT_CAPPED.value,
            IsolationControl.CANCEL_TERMINATION.value,
            IsolationControl.DESCENDANT_CLEANUP.value,
            IsolationControl.TENANT_ISOLATION.value,
        ],
        notes="Strong candidate; requires host administration for scheduling "
              "and patching. Reconsider if Cloud Run sandbox cannot meet the "
              "filesystem constraint with a minimal image.",
    ),
]


def isolation_decision_summary() -> Dict[str, Any]:
    """The §7.5 record for the ADR and the deployment service: managed
    execution is enabled ONLY when the selected backend is proven against
    ALL acceptance tests."""
    selected = next((d for d in RUNTIME_DECISIONS if d.status.startswith("selected")), None)
    required = {t.control.value for t in ISOLATION_ACCEPTANCE_TESTS}
    supported = set(selected.controls_supported) if selected else set()
    unproven = sorted(required - supported)
    return {
        "selected_backend": selected.backend if selected else None,
        "proven": bool(selected and selected.proven),
        "managed_execution_available": bool(selected and selected.proven),
        "controls_required": sorted(required),
        "controls_supported": sorted(supported),
        "controls_unproven": unproven,
        "acceptance_test_count": len(ISOLATION_ACCEPTANCE_TESTS),
        "note": ("Managed execution remains unavailable until the selected "
                 "backend passes every acceptance test in a live deployment "
                 "(§7.5 step 5)."),
    }


# ── §7.14/§7.15: the outbound operation broker contract ───────────────

class BrokerDecision(str, Enum):
    ALLOWED = "allowed"
    DENIED_DESTINATION = "denied_destination"
    DENIED_METHOD = "denied_method"
    DENIED_NOT_IN_ALLOWLIST = "denied_not_in_allowlist"
    DENIED_CAPABILITY_REPLAY = "denied_capability_replay"   # §7.15 acceptance
    DENIED_CAPABILITY_EXPIRED = "denied_capability_expired"
    DENIED_BUDGET_EXHAUSTED = "denied_budget_exhausted"
    DENIED_EFFECT_CONFLICT = "denied_effect_conflict"


@dataclass
class ExecutionCapability:
    """§7.15: a NARROW execution capability instead of a reusable provider
    secret. Bound to tenant, execution, artifact, operation, credential
    binding, expiry, and request budget. For writes, also the authorized
    effect. Replaying it from another execution/tenant/artifact is
    rejected."""
    capability_id: str
    tenant_id: str
    execution_id: str
    artifact_digest: str
    operation_name: str
    credential_binding_id: str
    allowed_destinations: List[str] = field(default_factory=list)
    allowed_methods: List[str] = field(default_factory=lambda: ["GET"])
    request_budget: int = 10
    effect_id: Optional[str] = None          # writes: the authorized effect
    expires_at: int = 0

    def replayed(self, *, tenant_id: str, execution_id: str,
                 artifact_digest: str) -> bool:
        """§7.15 acceptance: replaying a capability from another execution,
        tenant, or artifact is detected."""
        return (self.tenant_id != tenant_id
                or self.execution_id != execution_id
                or self.artifact_digest != artifact_digest)


def evaluate_broker_request(capability: ExecutionCapability, *, tenant_id: str,
                            execution_id: str, artifact_digest: str,
                            method: str, destination: str,
                            now: Optional[int] = None,
                            requests_used: int = 0) -> tuple[BrokerDecision, str]:
    """§7.14/§7.15: the broker verifies EVERYTHING before a provider request:
    tenant/execution/artifact identity, method and destination allowlists,
    capability expiry and replay, and request budget. NOT an unrestricted
    proxy - changing the generated source to remove its URL checks grants no
    wider network access (§7.14 acceptance)."""
    from backend.models.admission import epoch_now
    now = now if now is not None else epoch_now()
    if capability.replayed(tenant_id=tenant_id, execution_id=execution_id,
                           artifact_digest=artifact_digest):
        return (BrokerDecision.DENIED_CAPABILITY_REPLAY,
                "Capability replayed outside its authorized execution/tenant/"
                "artifact context (§7.15)")
    if capability.expires_at and now > capability.expires_at:
        return BrokerDecision.DENIED_CAPABILITY_EXPIRED, "Capability expired"
    if method.upper() not in [m.upper() for m in capability.allowed_methods]:
        return BrokerDecision.DENIED_METHOD, f"Method {method} not allowed"
    dest_allowed = any(
        destination == d or destination.startswith(d.rstrip("/") + "/")
        for d in capability.allowed_destinations)
    if not dest_allowed:
        return (BrokerDecision.DENIED_DESTINATION,
                f"Destination {destination} not in allowed list (§7.14: "
                "enforced at the boundary, not in generated source)")
    if requests_used >= capability.request_budget:
        return BrokerDecision.DENIED_BUDGET_EXHAUSTED, "Request budget exhausted"
    return BrokerDecision.ALLOWED, ""
