"""Runner model + resource profiles (Phase 7 §7.11/§7.12).

One runner CONTRACT, one selected backend at a time (§7.5: prove one before
several). Managed execution stays UNAVAILABLE until a backend satisfies the
isolation acceptance profile — the default implementation below is the
explicit "not supported" result, preserving the SandboxTester fallback.

Execution state is separate from cleanup state (§7.11): a failed process can
still have resources awaiting cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExecutionState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class CleanupState(str, Enum):
    """§7.11: independent from ExecutionState — a failed process can still
    have resources awaiting cleanup."""
    PENDING = "pending"
    CLEANED = "cleaned"
    ORPHANED = "orphaned"


# §7.12: runtime profiles with explicit limits. Enforcement happens OUTSIDE
# generated code (supervisor + runtime), never in-process.
RUNTIME_PROFILES: Dict[str, Dict[str, Any]] = {
    "protocol_test": {
        "wall_time_seconds": 60,
        "memory_mb": 256,
        "max_processes": 16,
        "workspace_mb": 8,
        "max_output_bytes": 256_000,
        "network": "none",
        "deadline_enforcer": "supervisor",
    },
    "pure_compute": {
        "wall_time_seconds": 30,
        "memory_mb": 256,
        "max_processes": 16,
        "workspace_mb": 8,
        "max_output_bytes": 128_000,
        "network": "none",
        "deadline_enforcer": "supervisor",
    },
    "read_connector": {
        "wall_time_seconds": 120,
        "memory_mb": 512,
        "max_processes": 16,
        "workspace_mb": 32,
        "max_output_bytes": 1_000_000,
        "network": "broker_only",   # §7.14: outbound via the operation broker
        "deadline_enforcer": "supervisor",
    },
    "write_connector": {
        "wall_time_seconds": 120,
        "memory_mb": 512,
        "max_processes": 16,
        "workspace_mb": 32,
        "max_output_bytes": 1_000_000,
        "network": "broker_only",
        "deadline_enforcer": "supervisor",
        "requires": "phase5_effect_boundary_verified",
    },
}


class RunnerUnavailableError(Exception):
    """Managed execution cannot start: no backend satisfies the required
    isolation profile for this deployment (§7.5 — keep managed execution
    unavailable if essential controls cannot be enforced)."""


@dataclass
class ExecutionRequest:
    """§7.11 — the runner's input. NO arbitrary shell command: the entrypoint
    comes from the verified artifact."""
    execution_id: str
    tenant_id: str
    artifact_digest: str
    runtime_image_digest: Optional[str]
    policy_profile_id: str                 # one of RUNTIME_PROFILES
    operation_allowlist: List[str] = field(default_factory=list)
    input_reference: Optional[str] = None
    deadline_seconds: int = 60
    resource_limits: Dict[str, Any] = field(default_factory=dict)
    network_grants: List[str] = field(default_factory=list)
    output_limits: Dict[str, int] = field(default_factory=dict)
    workflow_run_id: Optional[str] = None
    workflow_task_id: Optional[str] = None
    workflow_attempt: Optional[int] = None

    def __post_init__(self) -> None:
        profile = RUNTIME_PROFILES.get(self.policy_profile_id)
        if profile is None:
            raise ValueError(f"Unknown runtime profile: {self.policy_profile_id}")
        if profile.get("requires") and not profile["requires"] == "none":
            # write_connector requires the verified Phase 5 boundary (§6.2).
            if self.policy_profile_id == "write_connector":
                raise RunnerUnavailableError(
                    "write_connector profile requires the verified Phase 5 "
                    "effect boundary before managed write execution (§6.2/§7.3)")


@dataclass
class ExecutionRecord:
    """§7.11 — the durable lifecycle record."""
    execution_id: str
    request: ExecutionRequest
    state: ExecutionState = ExecutionState.QUEUED
    cleanup_state: CleanupState = CleanupState.PENDING
    runner_assignment: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[float] = None
    attempt: int = 1
    sandbox_identity: Optional[str] = None
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    exit_reason: Optional[str] = None
    output_references: List[str] = field(default_factory=list)


class IsolatedRunner:
    """§7.11 — the runner CONTRACT. The default implementation returns the
    explicit unsupported result: managed execution stays unavailable until a
    backend (Cloud Run sandbox, gVisor, microVM — §7.5) is proven against
    the isolation acceptance profile. submit_execution()/get_execution()/
    stream_execution_events()/request_cancellation()/terminate_execution()/
    collect_outputs()/cleanup_execution() define the interface every backend
    must satisfy."""

    name = "unavailable"

    async def submit_execution(self, request: ExecutionRequest) -> ExecutionRecord:
        raise RunnerUnavailableError(
            "Managed isolated execution is not available on this deployment: "
            "no runner backend has been proven against the required isolation "
            "profile (§7.5). Source export and declarative adapters remain "
            "fully supported.")

    async def get_execution(self, execution_id: str) -> Optional[ExecutionRecord]:
        return None

    async def request_cancellation(self, execution_id: str) -> bool:
        return False

    async def terminate_execution(self, execution_id: str) -> bool:
        return False

    async def collect_outputs(self, execution_id: str) -> List[Dict[str, Any]]:
        return []

    async def cleanup_execution(self, execution_id: str) -> bool:
        return False


_runner: Optional[IsolatedRunner] = None


def set_runner(runner: Optional[IsolatedRunner]) -> None:
    global _runner
    _runner = runner


def get_runner() -> IsolatedRunner:
    return _runner if _runner is not None else IsolatedRunner()
