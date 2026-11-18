"""
AgentOS — Webhooks Router

POST /api/v1/webhooks/{schedule_or_goal_id} — External trigger.
If the id matches one of the caller's schedules, its goal is executed with the
webhook payload injected as the root task's output ({{ tasks.webhook.output.* }}).
"""

import uuid
import logging
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException

from backend.models.schemas import WorkflowRun, Task, TaskStatus
from backend.engine.repo_adapter import persist_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/{schedule_id}")
async def trigger_webhook(schedule_id: str, request: Request):
    """
    Webhook trigger: starts the goal attached to the given schedule,
    with the webhook payload available to all tasks via interpolation.
    """
    factory = getattr(request.app.state, "factory", None)
    workflow_engine = getattr(request.app.state, "workflow_engine", None)
    if not factory or not workflow_engine:
        raise HTTPException(status_code=500, detail="Server not initialized")

    schedule = await factory.schedule_repo.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="No trigger registered with this id")
    from backend.security.rate_limiter import check_rate_limit, check_shared_rate_limit, get_client_identifier
    from backend.security.secrets_vault import secrets_vault
    from backend.security.webhook_signature import verify_signature
    check_rate_limit(get_client_identifier(request), "webhook")
    await check_shared_rate_limit(get_client_identifier(request), group="webhook")
    encrypted = await factory.secrets_repo.get_secret(schedule["user_id"], f"webhook:{schedule_id}")
    if not encrypted:
        raise HTTPException(status_code=403, detail="Webhook signing is not configured")
    body_limit = 1_000_000
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > body_limit:
        raise HTTPException(status_code=413, detail="Webhook body exceeds 1 MB")
    body_buffer = bytearray()
    async for chunk in request.stream():
        if len(body_buffer) + len(chunk) > body_limit:
            raise HTTPException(status_code=413, detail="Webhook body exceeds 1 MB")
        body_buffer.extend(chunk)
    body = bytes(body_buffer)
    timestamp = request.headers.get("x-agentos-timestamp", "")
    signature = request.headers.get("x-agentos-signature", "")
    secret = secrets_vault.decrypt(encrypted)
    if not verify_signature(secret, timestamp, signature, body):
        raise HTTPException(status_code=401, detail="Invalid or expired webhook signature")
    check_rate_limit(f"user:{schedule['user_id']}", "workflow")
    # Phase 3 §3.10: an accepted webhook starts a real run — the OWNER's
    # workflow allowance is enforced shared across replicas, not per-instance.
    await check_shared_rate_limit(f"user:{schedule['user_id']}", group="workflow")
    if str(schedule.get("status", "")).upper() != "ACTIVE":
        raise HTTPException(status_code=409, detail="Trigger is paused")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Webhook requires a JSON object")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook requires a JSON object")
    replay_key = "webhook:" + hashlib.sha256(f"{schedule_id}:{timestamp}:{signature}".encode()).hexdigest()
    if not await factory.idempotency_repo.atomic_insert(replay_key, schedule_id, "webhook"):
        raise HTTPException(status_code=409, detail="Webhook delivery already received")

    # §29.19 revision: a tenant/source-scoped BUSINESS DELIVERY ID (sender
    # header x-agentos-delivery-id, or a `delivery_id` payload field) makes
    # the sender's retry identity explicit. Retrying the SAME event with a
    # FRESH signature cannot create a second logical run; reusing one ID
    # with a DIFFERENT body is a conflict refusal. The recorded digest
    # makes the distinction durable. Supported on repositories with
    # atomic_insert + get_entry (SQLite/Firestore ledger); the signature
    # replay key remains the baseline dedup elsewhere.
    business_delivery_id = str(
        request.headers.get("x-agentos-delivery-id")
        or payload.get("delivery_id") or "").strip()[:200]
    body_digest = hashlib.sha256(body).hexdigest()
    biz_key = None
    if business_delivery_id and \
            hasattr(factory.idempotency_repo, "atomic_insert") and \
            hasattr(factory.idempotency_repo, "get_entry"):
        biz_key = "webhook-biz:" + hashlib.sha256(
            f"{schedule['user_id']}:{schedule_id}:{business_delivery_id}"
            .encode()).hexdigest()

        def _biz_conflict():
            return HTTPException(status_code=409, detail={
                "code": "BUSINESS_DELIVERY_CONFLICT",
                "accepted": False,
                "delivery_id": business_delivery_id,
                "message": "This delivery ID was already used with a "
                           "different body — a retried event must keep "
                           "its payload byte-identical or use a new ID."})

        existing = await factory.idempotency_repo.get_entry(biz_key)
        if existing is not None:
            if existing.get("task_id") == body_digest:
                return {"message": "Webhook delivery already received",
                        "run_id": (existing.get("result_payload")
                                   or existing.get("workflow_id") or None),
                        "delivery_id": business_delivery_id,
                        "deduplicated": True}
            raise _biz_conflict()
        if not await factory.idempotency_repo.atomic_insert(
                biz_key, schedule_id, body_digest):
            existing = await factory.idempotency_repo.get_entry(biz_key)
            if existing is not None and existing.get("task_id") != body_digest:
                raise _biz_conflict()
            return {"message": "Webhook delivery already received",
                    "run_id": (existing.get("result_payload")
                               if existing else None),
                    "delivery_id": business_delivery_id,
                    "deduplicated": True}
    # §4.16: if acceptance FAILS below, the replay key is RELEASED so the
    # sender's retry can be accepted — a delivery's retry identity is never
    # permanently consumed by a temporary capacity/admission failure.

    # Phase 4 §4.16: ATOMIC delivery acceptance — command + queue admission
    # + the run/tasks + the outbox event commit in ONE transaction. A replay
    # key can never consume a delivery's retry identity without a command
    # existing (§4.16's failed-sequence defect), and a crash between
    # acceptance and run-creation is recovered by the reconciler/outbox
    # because the run rows committed WITH the command.
    from backend.services import command_service
    from backend.config.settings import settings as app_settings
    import json as _json

    run_id = str(uuid.uuid4())
    workflow_id = f"wh-{run_id[:8]}"
    owner = schedule["user_id"]
    now_iso = datetime.now(timezone.utc).isoformat()

    async def _insert_webhook_work(conn):
        """The initial work INSIDE the acceptance transaction (§4.5): the
        run row and its two tasks, create-if-absent semantics (INSERT OR
        IGNORE) so an outbox-driven re-drive cannot duplicate the run."""
        conn_obj = conn
        await conn_obj.execute(
            """
            INSERT OR IGNORE INTO workflow_runs
                (run_id, workflow_id, user_id, goal, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'RUNNING', ?, ?)
            """,
            (run_id, workflow_id, owner, schedule["goal"], now_iso, now_iso))
        await conn_obj.execute(
            """
            INSERT OR IGNORE INTO tasks
                (task_id, run_id, workflow_id, user_id, agent, tool, status,
                 input_data, output_data, dependencies)
            VALUES ('webhook', ?, ?, ?, 'core.set', NULL, 'COMPLETED', ?, ?, '[]')
            """,
            (run_id, workflow_id, owner, _json.dumps({"fields": payload}),
             _json.dumps(payload)))
        await conn_obj.execute(
            """
            INSERT OR IGNORE INTO tasks
                (task_id, run_id, workflow_id, user_id, agent, tool, status,
                 input_data, output_data, dependencies)
            VALUES (?, ?, ?, ?, 'OrchestratorAgent', NULL, 'PENDING', ?, NULL, ?)
            """,
            (f"root-{run_id[:8]}", run_id, workflow_id, owner,
             _json.dumps({"goal": schedule["goal"], "webhook_payload": payload}),
             _json.dumps(["webhook"])))

    try:
        receipt = await command_service.accept_command(
            command_type="webhook",
            owner_user_id=owner,
            actor_type="webhook",
            source="webhook",
            idempotency_key=replay_key,
            request_digest=hashlib.sha256(body).hexdigest(),
            payload={"schedule_id": schedule_id, "goal": schedule["goal"],
                     "fields": payload},
            run_id=run_id,
            max_pending_per_user=app_settings.TENANT_MAX_PENDING_COMMANDS,
            max_wait_seconds=2 * 3600,
            initial_work=_insert_webhook_work,
        )
    except RuntimeError:
        if hasattr(factory.idempotency_repo, "release_replay_key"):
            await factory.idempotency_repo.release_replay_key(replay_key)
        if biz_key is not None and hasattr(factory.idempotency_repo, "release_replay_key"):
            await factory.idempotency_repo.release_replay_key(biz_key)
        raise  # acceptance infrastructure missing: honest 500
    if not receipt.accepted:
        # §4.16: acceptance failed (queue full, admission unavailable) —
        # RELEASE the replay key so the sender's retry can be accepted.
        # A delivery's retry identity is never permanently consumed by a
        # temporary failure.
        if hasattr(factory.idempotency_repo, "release_replay_key"):
            await factory.idempotency_repo.release_replay_key(replay_key)
        if biz_key is not None and hasattr(factory.idempotency_repo, "release_replay_key"):
            await factory.idempotency_repo.release_replay_key(biz_key)
        code = receipt.error_code.value if receipt.error_code else "REJECTED"
        if code == "IDEMPOTENCY_CONFLICT":
            raise HTTPException(status_code=409, detail={
                "code": code, "accepted": False,
                "message": receipt.message or "Delivery conflicts with an accepted command",
                "command_id": receipt.command_id})
        raise HTTPException(status_code=429, detail={
            "code": code, "accepted": False,
            "message": receipt.message or "The schedule owner's workspace has "
                       "too much work waiting; this delivery was not accepted.",
            "command_id": receipt.command_id})
    if receipt.deduplicated:
        # §4.16: duplicate accepted delivery → the original receipt, per the
        # webhook contract (never a second logical workflow).
        return {"message": "Webhook delivery already received",
                "run_id": receipt.run_id, "command_id": receipt.command_id,
                "deduplicated": True}
    run_id = receipt.run_id or run_id
    logger.info(f"Webhook accepted command {receipt.command_id} → run {run_id}")

    await workflow_engine.evaluate_dag(run_id)
    await factory.idempotency_repo.commit_success(replay_key, run_id)
    if biz_key is not None:
        await factory.idempotency_repo.commit_success(biz_key, run_id)

    response = {"message": "Webhook received", "run_id": run_id,
                "workflow_id": workflow_id, "command_id": receipt.command_id}
    if business_delivery_id:
        response["delivery_id"] = business_delivery_id
    return response
