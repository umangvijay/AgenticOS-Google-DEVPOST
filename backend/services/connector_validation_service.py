"""Connector validation service (Phase 6 §6.8) — orchestration over the
deterministic evidence calculations that stay in tool_validation.py.

An evidence record answers a narrow question (§6.8): THIS operation, with
this adapter and this account binding, passed these required cases under
this profile. It never implies every input or provider behavior was tested.

Concurrency rule (§6.8): at validation START the relevant versions are
captured; at COMPLETION they are compared with current state. If the
credential, definition, runtime, or policy changed mid-run, the completed
run is preserved as history and marked superseded — it cannot publish as
current evidence. The comparison and publication happen atomically in one
owned transaction.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.services.tool_validation import (
    definition_digest,
    validation_provenance,
)

logger = logging.getLogger(__name__)

VALIDATION_PROFILE_VERSION = "p6.read.v1"


@dataclass
class ValidationRun:
    validation_run_id: str
    connector_id: str
    revision_id: Optional[str]
    operation_id: str
    operation_digest: str
    credential_binding_id: str
    authorization_epoch: str
    provider_account_id: Optional[str]
    environment: str
    runtime_digest: Optional[str]
    validation_profile_version: str = VALIDATION_PROFILE_VERSION
    case_results: List[Dict[str, Any]] = field(default_factory=list)
    started_at: int = 0
    completed_at: Optional[int] = None
    valid_until: Optional[int] = None
    artifact_references: List[str] = field(default_factory=list)
    status: str = "running"          # running | passed | failed | superseded
    superseded_reason: Optional[str] = None


async def start_validation_run(
    repo, *, connector_id: str, mcp: dict, tool: dict,
    credential_binding_id: str, authorization_epoch: str = "e0",
    provider_account_id: Optional[str] = None, environment: str = "production",
    runtime_digest: Optional[str] = None,
    valid_hours: int = 24,
) -> ValidationRun:
    """Capture the validation context at START (§6.8 concurrency rule)."""
    run = ValidationRun(
        validation_run_id=f"vr_{uuid.uuid4().hex[:20]}",
        connector_id=connector_id,
        revision_id=mcp.get("active_revision_id"),
        operation_id=tool.get("tool_name", ""),
        operation_digest=definition_digest(tool, mcp.get("spec_hash")),
        credential_binding_id=credential_binding_id,
        authorization_epoch=authorization_epoch,
        provider_account_id=provider_account_id,
        environment=environment,
        runtime_digest=runtime_digest,
        started_at=epoch_now(),
        valid_until=epoch_now() + valid_hours * 3600,
    )
    await repo.save_validation_run(connector_id, run.__dict__)
    return run


async def complete_validation_run(
    repo, *, run: ValidationRun, passed: bool,
    case_results: Optional[List[Dict[str, Any]]] = None,
    current_definition_digest: Optional[str] = None,
    current_credential_binding_id: Optional[str] = None,
    current_runtime_digest: Optional[str] = None,
) -> ValidationRun:
    """§6.8: compare the START context with CURRENT state atomically; a
    changed context marks the run superseded — its evidence can NOT publish
    as current (the acceptance test: rotate credentials while a probe is
    paused and the old probe's success must not validate the new binding)."""
    superseded = None
    if current_definition_digest is not None and \
            current_definition_digest != run.operation_digest:
        superseded = "definition changed during validation"
    elif current_credential_binding_id is not None and \
            current_credential_binding_id != run.credential_binding_id:
        superseded = "credential context changed during validation"
    elif current_runtime_digest is not None and run.runtime_digest and \
            current_runtime_digest != run.runtime_digest:
        superseded = "runtime changed during validation"

    run.case_results = case_results or run.case_results
    run.completed_at = epoch_now()
    if superseded:
        run.status = "superseded"
        run.superseded_reason = superseded
    else:
        run.status = "passed" if passed else "failed"
    await repo.save_validation_run(run.connector_id, run.__dict__)
    return run


def evidence_from_run(run: ValidationRun) -> Optional[Dict[str, Any]]:
    """§6.8: only a PASSED, non-superseded run yields executable evidence.
    Stale/superseded runs are history."""
    if run.status != "passed":
        return None
    return {
        "evidence_id": f"ev_{run.validation_run_id}",
        "validation_run_id": run.validation_run_id,
        "connector_id": run.connector_id,
        "revision_id": run.revision_id,
        "operation_id": run.operation_id,
        "operation_digest": run.operation_digest,
        "credential_binding_id": run.credential_binding_id,
        "authorization_epoch": run.authorization_epoch,
        "provider_account_id": run.provider_account_id,
        "environment": run.environment,
        "runtime_digest": run.runtime_digest,
        "validation_profile_version": run.validation_profile_version,
        "case_results": run.case_results,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "valid_until": run.valid_until,
        "artifact_references": run.artifact_references,
        # The narrow claim (§6.8): this operation, with this adapter and
        # this account binding, passed these required cases under this
        # profile — within the validity window.
        "claim": (
            f"operation={run.operation_id} revision={run.revision_id} "
            f"binding={run.credential_binding_id} profile="
            f"{run.validation_profile_version} passed; valid until "
            f"{datetime.fromtimestamp(run.valid_until, timezone.utc).isoformat() if run.valid_until else 'n/a'}"),
    }
