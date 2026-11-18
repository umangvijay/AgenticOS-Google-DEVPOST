"""Operational controls for standalone MCP execution (Phase 7 §7.24).

Kill switches that block NEW dispatches and revoke execution capabilities
while accounting for effects already in progress — never deleting evidence:

- disable new generated-code executions (global)
- revoke a particular artifact
- revoke a runtime image
- cancel a tenant's active executions
- stop provider access (broker) without deleting evidence
- drain a runner version during upgrade

Telemetry counters (§7.24) use bounded labels (profile, reason) — command/
artifact/execution IDs correlate via access-controlled logs, not metric
labels.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

METRICS: Dict[str, int] = {}


def _incr(name: str, labels: Optional[Dict[str, str]] = None) -> None:
    key = name
    if labels:
        flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
        key = f"{name}{{{flat}}}"
    METRICS[key] = METRICS.get(key, 0) + 1


def runner_metrics() -> Dict[str, int]:
    return dict(METRICS)


class ExecutionControls:
    """§7.24 kill switches. State is process-local by default; deployments
    with multiple control planes should back this with the durable store —
    the interface is the contract."""

    def __init__(self) -> None:
        self.new_executions_disabled = False
        self.revoked_artifacts: Dict[str, str] = {}    # digest → reason
        self.revoked_images: Dict[str, str] = {}       # digest → reason
        self.provider_access_stopped = False
        self.drained_runner_versions: Dict[str, str] = {}

    def disable_new_executions(self, reason: str) -> None:
        self.new_executions_disabled = True
        _incr("kill_switch", {"switch": "new_executions"})
        logger.warning("Kill switch: new generated-code executions disabled (%s)",
                       reason)

    def enable_new_executions(self) -> None:
        self.new_executions_disabled = False

    def revoke_artifact(self, artifact_digest: str, reason: str) -> None:
        """§7.24: revoke a particular artifact — blocks new dispatches; the
        revocation stays visible (never deletes evidence)."""
        self.revoked_artifacts[artifact_digest] = reason
        _incr("artifact_revoked")
        logger.warning("Artifact %s revoked: %s", artifact_digest[:16], reason)

    def revoke_runtime_image(self, image_digest: str, reason: str) -> None:
        self.revoked_images[image_digest] = reason
        _incr("runtime_image_revoked")
        logger.warning("Runtime image %s revoked: %s", image_digest[:16], reason)

    def stop_provider_access(self, reason: str) -> None:
        """§7.24: stop provider access WITHOUT deleting evidence."""
        self.provider_access_stopped = True
        _incr("provider_access_stopped")
        logger.warning("Provider access stopped: %s", reason)

    def drain_runner_version(self, version: str, reason: str) -> None:
        self.drained_runner_versions[version] = reason
        _incr("runner_version_drained", {"version": version[:12]})

    def check_dispatch(self, *, artifact_digest: str,
                       runtime_image_digest: Optional[str],
                       runner_version: Optional[str] = None) -> tuple[bool, List[str]]:
        """Pre-dispatch gate: every kill switch consulted BEFORE a new
        execution starts (§7.24: a kill switch must block new dispatches)."""
        reasons = []
        if self.new_executions_disabled:
            reasons.append("NEW_EXECUTIONS_DISABLED")
        if artifact_digest in self.revoked_artifacts:
            reasons.append("ARTIFACT_REVOKED: "
                           + self.revoked_artifacts[artifact_digest])
        if runtime_image_digest and runtime_image_digest in self.revoked_images:
            reasons.append("RUNTIME_IMAGE_REVOKED: "
                           + self.revoked_images[runtime_image_digest])
        if runner_version and runner_version in self.drained_runner_versions:
            reasons.append("RUNNER_VERSION_DRAINED")
        if reasons:
            _incr("dispatch_blocked", {"reason": reasons[0].split(":")[0]})
        return (not reasons), reasons

    def check_broker(self) -> tuple[bool, str]:
        if self.provider_access_stopped:
            return False, "Provider access stopped by operational control"
        return True, ""


_controls: Optional[ExecutionControls] = None


def get_execution_controls() -> ExecutionControls:
    global _controls
    if _controls is None:
        _controls = ExecutionControls()
    return _controls


def set_execution_controls(controls: ExecutionControls) -> None:
    global _controls
    _controls = controls


async def cancel_tenant_executions(execution_repo, tenant_id: str,
                                   reason: str) -> int:
    """§7.24: cancel a tenant's active executions — new dispatches stop,
    already-submitted effects keep their truthful state (§7.18 step 7)."""
    active = await execution_repo.executions_for_tenant(tenant_id, limit=100)
    cancelled = 0
    for rec in active:
        if rec.get("state") in ("queued", "starting", "running"):
            if await execution_repo.cancel_pending(rec["execution_id"]):
                await execution_repo.mark_terminated(
                    rec["execution_id"], f"tenant cancellation: {reason}")
                cancelled += 1
    if cancelled:
        _incr("tenant_executions_cancelled", amount=cancelled)
    return cancelled


async def reconcile_orphans(execution_repo, limit: int = 50) -> Dict[str, int]:
    """§7.11/§7.18/§7.24: the worker-ticker pass — executions whose lease
    expired while still running are marked orphaned and moved to cleanup;
    terminal executions pending cleanup are cleaned. Never deletes
    evidence."""
    report = {"orphans_marked": 0, "cleaned": 0}
    for rec in await execution_repo.orphaned_executions(limit=limit):
        await execution_repo.save_execution({**rec, "state": "superseded",
                                             "exit_reason": "lease lost (worker crash?)",
                                             "cleanup_state": "pending"})
        report["orphans_marked"] += 1
        _incr("orphaned_executions")
    for rec in await execution_repo.pending_cleanup(limit=limit):
        await execution_repo.mark_cleaned(rec["execution_id"])
        report["cleaned"] += 1
    return report
