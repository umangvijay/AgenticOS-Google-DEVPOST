"""Shared durable connector-build entry point (audit G07).

Two surfaces can request an integration build — the integrations API and
the orchestrator's build_integration tool. Both now go through this
service so a build has ONE durable record with its origin and (when it
came from autonomous execution) the parent run that is waiting on it:

- dispatch mode (API): create the record, persist a workflow task, and
  hand it to the engine/bus — worker restarts cannot lose it.
- track mode (orchestrator): create the record and hand its id to the
  inline factory run, so stage logs and the final outcome land in the
  same durable place and the parent run is recoverable from the record.

Phase 3 §3.10/§3.12: BOTH surfaces pass through the same admission gate
HERE, at the service funnel — shared cross-replica rate admission (the
mcp_build policy, fail-closed on store outage) and bounded durable command
admission (one logical build per user+method+source; tenant pending caps).
Putting the gate in the service (not the router) is what makes
agent-requested builds obey the same controls as user-triggered ones
(§3.16: subagents must not bypass normal-user limits).
"""
import hashlib
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import AdmissionReason
from backend.services.app_intake import intake_at_admission

logger = logging.getLogger(__name__)

ORIGIN_API = "api"
ORIGIN_ORCHESTRATOR = "orchestrator"


class BuildAdmissionDenied(Exception):
    """Typed admission refusal for a build request (§3.8 contract in
    exception form). `retry_after_seconds` carries real timing; routers map
    reason → HTTP status (429 / 409 for duplicates)."""

    def __init__(self, reason: AdmissionReason, message: str,
                 retry_after_seconds: int = 0, first_command_id: str = "",
                 decision_id: str = ""):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after_seconds = max(0, int(retry_after_seconds))
        self.first_command_id = first_command_id
        self.decision_id = decision_id


def build_idempotency_key(user_id: str, method: str, source: str) -> str:
    """One logical build per (user, method, source): a retried identical
    build — from either surface — returns the existing admission instead of
    consuming another queue slot (§3.10 command admission vs request rate).
    §4.0D (R8): the stored key is additionally TENANT-SCOPED by the
    repository, and the payload digest below catches same-key/different-
    payload reuse as an explicit conflict."""
    return "mcp_build:" + hashlib.sha256(
        f"{user_id}|{method}|{source}".encode()).hexdigest()[:32]


def build_request_digest(method: str, source: str, name: Optional[str]) -> str:
    return hashlib.sha256(f"{method}|{source}|{name or ''}".encode()).hexdigest()


async def admit_build(factory, *, user_id: str, method: str, source: str,
                      origin: str, name: Optional[str] = None) -> str:
    """The ONE admission gate every build passes. Returns the command id to
    settle once the durable record exists. Raises BuildAdmissionDenied."""
    from backend.security.rate_limiter import admission_decision

    decision = await admission_decision(f"user:{user_id}", "mcp_build")
    if not decision.allowed:
        raise BuildAdmissionDenied(
            decision.reason, decision.to_detail()["message"],
            retry_after_seconds=decision.retry_after_seconds,
            decision_id=decision.decision_id)

    from backend.services import admission_service
    command_id = str(uuid.uuid4())
    admission = await admission_service.admit_command(
        command_id, user_id, None, "build",
        idempotency_key=build_idempotency_key(user_id, method, source),
        request_digest=build_request_digest(method, source, name),
        max_wait_seconds=2 * 3600)
    if not admission.accepted:
        raise BuildAdmissionDenied(
            admission.reason,
            "Your workspace has too many builds waiting; retry after some complete.",
            retry_after_seconds=admission.retry_after_seconds)
    if admission.duplicate:
        raise BuildAdmissionDenied(
            AdmissionReason.RATE_LIMITED,
            "An identical build is already queued or running.",
            first_command_id=admission.first_command_id)
    return command_id


async def settle_build_command(command_id: Optional[str]) -> None:
    """Free the queue slot when the durable record could not be created."""
    if not command_id:
        return
    from backend.services import admission_service
    try:
        await admission_service.settle_command(command_id)
    except Exception:
        logger.warning("Failed to settle build command %s", command_id, exc_info=True)


async def create_build_record(
    factory,
    *,
    user_id: str,
    method: str,
    source: str,
    name: Optional[str],
    origin: str,
    parent_run_id: Optional[str] = None,
    parent_workflow_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    wait_reason: str = "capability_missing",
    organization_id: Optional[str] = None,
    credential_reference: Optional[str] = None,
    required_operations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    # §12.6: organization scope and the credential-BINDING REFERENCE (never
    # a raw secret) travel with the build end to end.
    build_id = str(uuid.uuid4())
    # §29.14: the application-intake record is persisted BEFORE any
    # generation runs — what was supplied, its digest, what the proposal
    # can honestly support, and what is still unknown.
    from backend.services.app_intake import intake_at_admission
    intake = intake_at_admission(
        method=method, source=source, name=name,
        required_operations=required_operations,
        credential_reference=credential_reference, origin=origin)
    await factory.mcp_repo.create_build({
        "build_id": build_id,
        "user_id": user_id,
        "name": name or "",
        "method": method,
        "source": (source or "")[:2000],
        "status": "queued",
        "stage": "queued",
        # Provenance for recovery (audit G07 / v5.6 A10): who asked, and
        # exactly which run+task is waiting for this capability.
        "origin": origin,
        "parent_run_id": parent_run_id,
        "parent_workflow_id": parent_workflow_id,
        "parent_task_id": parent_task_id,
        "wait_reason": wait_reason,
        "organization_id": organization_id,
        "credential_reference": credential_reference,
        "required_operations": required_operations,
        "intake": intake,
    })
    return {"build_id": build_id, "intake": intake}


async def request_durable_build(
    factory,
    *,
    user_id: str,
    method: str,
    source: str,
    name: Optional[str],
    origin: str = ORIGIN_API,
    parent_run_id: Optional[str] = None,
    parent_workflow_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
    workflow_engine=None,
    organization_id: Optional[str] = None,
    credential_reference: Optional[str] = None,
    required_operations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Dispatch mode: ONE atomic acceptance (§4.12/§4.5) — the shared
    admission gate, the command row, the BUILD record, the workflow run and
    its build task, and the outbox event commit in a single transaction.
    Publication failures can no longer leave conflicting persisted state:
    the outbox event IS the dispatch intent, and the reconciler re-drives
    any accepted-but-undispatched build.
    """
    import json as _json
    from datetime import datetime, timezone
    from backend.config.settings import settings

    # The shared cross-replica mcp_build allowance (fail-closed on store
    # outage, §3.9) — its own owned transaction in the rate-limit store.
    from backend.security.rate_limiter import admission_decision
    from backend.models.admission import AdmissionReason
    decision = await admission_decision(f"user:{user_id}", "mcp_build")
    if not decision.allowed:
        raise BuildAdmissionDenied(
            decision.reason, decision.to_detail()["message"],
            retry_after_seconds=decision.retry_after_seconds,
            decision_id=decision.decision_id)

    build_id = str(uuid.uuid4())
    run_id = f"mcp-{build_id}"
    workflow_id = f"wf-{build_id[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    from backend.models.command import new_command_id, new_wait_id
    build_command_id = new_command_id()
    # §12.6: the durable build contract carries organization scope and the
    # credential-BINDING REFERENCE (a name the execution boundary resolves
    # — never the secret itself, which stays out of model-visible payloads).
    build_row = {
        "build_id": build_id, "user_id": user_id, "name": name or "",
        "method": method, "source": (source or "")[:2000],
        "origin": origin, "parent_run_id": parent_run_id,
        "parent_workflow_id": parent_workflow_id,
        "parent_task_id": parent_task_id, "wait_reason": "capability_missing",
        "organization_id": organization_id,
        "credential_reference": credential_reference,
        # §13.3: the required operations travel with the build — the
        # readiness gate refuses UNDECLARED coverage.
        "required_operations": list(required_operations or []),
        # §29.14: the intake record persists BEFORE any generation runs.
        "intake": intake_at_admission(
            method=method, source=source, name=name,
            required_operations=required_operations,
            credential_reference=credential_reference, origin=origin),
    }
    run_row = {
        "run_id": run_id, "workflow_id": workflow_id, "user_id": user_id,
        "goal": f"Build MCP integration {name or source[:160]}",
        "created_at": now_iso, "updated_at": now_iso,
        "parent_run_id": parent_run_id, "thread_id": run_id,
        "org_id": organization_id,
    }
    task_row = {
        "task_id": "mcp-build", "run_id": run_id, "workflow_id": workflow_id,
        "user_id": user_id, "agent": "core.mcp_build", "tool": "mcp_build",
        "input_data": _json.dumps({
            "method": method, "source": source, "name": name or "",
            "build_id": build_id, "intent": source[:4000],
            # Credential binding REFERENCE — resolved at the execution
            # boundary, never the secret (§12.6).
            "credential_reference": credential_reference,
            "required_operations": list(required_operations or []),
        }),
        "dependencies": "[]", "timeout_seconds": 300, "max_retries": 2,
        "recovery_enabled": 1,
    }

    # §12.8 (finding I): ONE backend-neutral initial-work description for
    # both stores — the service translates it per store with equivalent
    # transactional behavior (create-if-absent inside the acceptance
    # transaction; the parent WAIT commits in the SAME transaction, C8).
    initial_work_items = []
    if parent_run_id and parent_task_id:
        initial_work_items.append(("parent_wait", {
            "wait_id": new_wait_id(),
            "parent_run_id": parent_run_id,
            "parent_task_id": parent_task_id,
            "child_command_id": build_command_id,
            "child_run_id": run_id,
            "condition": "child_build_succeeded",
            "state": "waiting",
            "created_at": now_iso,
        }))
    initial_work_items.append(("build_record", dict(build_row,
                                                    created_at=now_iso,
                                                    updated_at=now_iso)))
    initial_work_items.append(("workflow_run_with_task",
                               {"run": run_row, "task": task_row}))

    from backend.services.command_service import accept_command
    receipt = await accept_command(
        command_id=build_command_id,
        command_type="build",
        owner_user_id=user_id,
        organization_id=organization_id,
        actor_type="agent" if origin == ORIGIN_ORCHESTRATOR else "user",
        source=origin,
        idempotency_key=build_idempotency_key(user_id, method, source),
        request_digest=build_request_digest(method, source, name),
        payload={"method": method, "source": source[:4000], "name": name or "",
                 "build_id": build_id, "origin": origin,
                 "organization_id": organization_id,
                 "credential_reference": credential_reference,
                 # §14.H: the operation declaration is part of the durable
                 # build contract — the command payload carries it.
                 "required_operations": list(required_operations or [])},
        run_id=run_id,
        build_id=build_id,
        parent_command_id=None,
        parent_run_id=parent_run_id,
        parent_task_id=parent_task_id,
        max_pending_per_user=settings.TENANT_MAX_PENDING_COMMANDS,
        max_wait_seconds=2 * 3600,
        initial_work_items=initial_work_items,
    )
    if not receipt.accepted:
        raise BuildAdmissionDenied(
            AdmissionReason.QUEUE_FULL if receipt.error_code is None else
            _receipt_code_to_reason(receipt.error_code),
            receipt.message or "Build not accepted",
            first_command_id=receipt.command_id)
    if receipt.deduplicated:
        # A retried identical build: the ORIGINAL command/build owns this.
        raise BuildAdmissionDenied(
            AdmissionReason.RATE_LIMITED,
            "An identical build is already queued or running.",
            first_command_id=receipt.command_id)

    # Fast-path dispatch; the outbox event is the durable recovery path.
    if workflow_engine is not None:
        try:
            await workflow_engine.evaluate_dag(run_id)
        except Exception:
            logger.warning("Fast-path dispatch failed for build %s; the "
                           "outbox/reconciler re-drives it", build_id[:8],
                           exc_info=True)

    return {
        "build_id": build_id,
        "run_id": run_id,
        "command_id": receipt.command_id,
        "status": "queued",
        "origin": origin,
        "parent_run_id": parent_run_id,
    }


def _receipt_code_to_reason(code) -> AdmissionReason:
    from backend.models.admission import AdmissionReason as _AR
    try:
        return _AR(code.value)
    except Exception:
        return _AR.QUEUE_FULL


async def track_inline_build(
    factory,
    *,
    user_id: str,
    method: str,
    source: str,
    name: Optional[str],
    origin: str = ORIGIN_ORCHESTRATOR,
    parent_run_id: Optional[str] = None,
    parent_workflow_id: Optional[str] = None,
    parent_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Track mode (orchestrator): the SAME atomic acceptance — the build
    record persists with the command; the caller runs the factory inline and
    every stage lands on this durable record. No separate admission write
    exists anymore (§4.12 #1 fixed at the root)."""
    receipt_result = await request_durable_build(
        factory, user_id=user_id, method=method, source=source, name=name,
        origin=origin, parent_run_id=parent_run_id,
        parent_workflow_id=parent_workflow_id, parent_task_id=parent_task_id,
        workflow_engine=None,   # track mode: no fast-path dispatch
    )
    return {"build_id": receipt_result["build_id"],
            "command_id": receipt_result["command_id"],
            "run_id": receipt_result["run_id"]}
