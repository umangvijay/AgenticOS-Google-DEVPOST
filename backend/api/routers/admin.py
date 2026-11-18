"""AgentOS — Admin console API (admin role only).

GET    /api/v1/admin/users            — list users with usage in the 5h window
POST   /api/v1/admin/users/{id}/disable | enable — gate account access
GET    /api/v1/admin/stats            — platform-wide counters
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.api.dependencies.auth import require_admin, AuthenticatedUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

WINDOW_HOURS = 5


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory




def _require_super(admin) -> None:
    if getattr(admin, "role", "") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")

@router.get("/users")
async def list_users(request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()

    users: List[Dict[str, Any]] = []
    repo = factory.user_repo
    if hasattr(repo, "list_users"):
        raw_users = await repo.list_users()
    else:
        raw_users = []
    for u in raw_users:
        user_id = u.get("user_id") or u.get("id") or ""
        entry = {
            "user_id": user_id,
            "email": u.get("email"),
            "name": u.get("name"),
            "role": u.get("role", "user"),
            "is_active": u.get("is_active", True),
            "created_at": u.get("created_at"),
        }
        try:
            runs = await factory.workflow_repo.list_runs(user_id, limit=200, offset=0)
            window_runs = [r for r in runs if str(r.get("created_at") or "") >= cutoff]
            entry["runs_total"] = len(runs)
            try:
                entry["mcps"] = len(await factory.mcp_repo.list_mcps(user_id=user_id))
            except Exception:
                entry["mcps"] = None
            entry["runs_5h"] = len(window_runs)
            entry["last_run_at"] = max((str(r.get("created_at") or "") for r in runs), default=None) or None
        except Exception:
            entry["runs_total"] = None
            entry["runs_5h"] = None
        users.append(entry)
    return {"users": users, "count": len(users), "window_hours": WINDOW_HOURS}


@router.get("/budget/metrics")
async def budget_accounting_metrics(admin = Depends(require_admin)):
    """Accounting observability (Phase 2, §2.19): admission outcomes,
    settlement failures, overruns, duplicate-suppression, uncertain holds,
    and the latest reconciliation sweep — the accounting-reliability surface.
    Counters carry no prompts, credentials, or raw user identifiers."""
    from backend.services.budget_service import budget_metrics
    from backend.config.settings import settings as app_settings
    return {
        "metrics": budget_metrics(),
        "enforcement_mode": app_settings.BUDGET_ENFORCEMENT_MODE,
    }


@router.post("/budget/reconcile")
async def trigger_budget_reconciliation(admin = Depends(require_admin)):
    """Operator-triggered reconciliation investigation (§2.9/§2.19): stale
    reserved/dispatching holds are marked uncertain — expiry triggers
    investigation, never unconditional release."""
    from backend.services.budget_service import get_budget_service
    report = await get_budget_service().sweep_for_reconciliation(
        older_than_minutes=60, limit=100,
    )
    return report


@router.get("/admission/metrics")
async def admission_metrics(admin = Depends(require_admin)):
    """Admission & capacity observability (Phase 3, §3.20).

    Reveals broken enforcement: admissions/denials per policy, shared-store
    latency and errors, LOCAL FALLBACK ACTIVATIONS (alert: strict limits
    degraded to per-instance), capacity denials, ACTIVE LEASES BY RESOURCE
    CLASS, lease expiries (workers dying mid-flight), per-class PENDING
    QUEUE depth with OLDEST WAIT, provider retry volume, and duplicate
    command suppression. Labels carry group/class/reason only — never user
    ids or command ids (those stay in access-controlled logs, correlatable
    via decision_id)."""
    from backend.security.shared_rate_limiter import admission_metrics
    from backend.services.capacity_service import capacity_metrics, get_capacity_repo
    from backend.services.admission_service import admission_service_metrics, queue_stats
    from backend.services.provider_backoff import retry_metrics
    from backend.services.command_service import command_service_metrics, get_command_repo
    from backend.services.command_reconciler import reconciler_metrics
    from backend.services.outbox_dispatcher import outbox_metrics
    from backend.security.admission_policy import all_policies

    repo = get_capacity_repo()
    active_by_class = {}
    if repo is not None:
        try:
            active_by_class = await repo.active_units_by_class()
        except Exception:
            pass
    outbox = {}
    command_repo = get_command_repo()
    if command_repo is not None:
        try:
            outbox = await command_repo.outbox_stats()
        except Exception:
            outbox = {}
    return {
        "rate_limiting": admission_metrics(),
        "capacity": {**capacity_metrics(), "active_units_by_class": active_by_class},
        "commands": {**admission_service_metrics(), "queue_by_class": queue_stats(),
                     "lifecycle": command_service_metrics()},
        "outbox": {**outbox, "dispatcher": outbox_metrics()},
        "reconciliation": reconciler_metrics(),
        "provider_retries": retry_metrics(),
        "policies": {
            group: {
                "limit": p.limit,
                "window_seconds": p.window_seconds,
                "identity": p.identity.value,
                "shared": p.shared,
                "on_store_failure": p.on_store_failure.value,
                "policy_version": p.policy_version,
            }
            for group, p in all_policies().items()
        },
    }


@router.post("/admission/policies/{group}/disable")
async def disable_admission_policy(group: str, request: Request, admin = Depends(require_admin)):
    """Emergency stop for one admission policy (Phase 3, §3.20): sets its
    limit to 0 so the group admits NOTHING (an explicit disabled policy, not
    an ambiguous magic number). Audited; reversible via /enable; there is
    deliberately NO 'reset all limits' endpoint."""
    from backend.models.admission import AdmissionPolicy, AdmissionPolicyError
    from backend.security.admission_policy import resolve_policy, register_policy
    try:
        current = resolve_policy(group)
    except AdmissionPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if current.disabled:
        return {"group": group, "limit": 0, "already_disabled": True}
    disabled = AdmissionPolicy(
        group=current.group, limit=0, window_seconds=current.window_seconds,
        algorithm=current.algorithm, identity=current.identity,
        shared=current.shared, on_store_failure=current.on_store_failure,
        http_status_denied=current.http_status_denied,
        policy_version=f"{current.policy_version}+disabled",
    )
    register_policy(disabled)
    factory = _get_factory(request)
    await factory.audit_repo.log_event({
        "event_type": "ADMISSION_POLICY_DISABLED", "actor_id": admin.user_id,
        "actor_type": "USER", "resource_id": group,
        "details": {"previous_limit": current.limit, "previous_policy_version": current.policy_version},
    })
    return {"group": group, "limit": 0, "previous_limit": current.limit,
            "policy_version": disabled.policy_version, "audited": True}


@router.post("/admission/policies/{group}/enable")
async def enable_admission_policy(group: str, request: Request, admin = Depends(require_admin)):
    """Restore a policy's settings-configured limit (undo /disable). Audited."""
    from backend.models.admission import AdmissionPolicy, AdmissionPolicyError
    from backend.security.admission_policy import resolve_policy, register_policy
    from backend.config.settings import settings as app_settings
    try:
        current = resolve_policy(group)
    except AdmissionPolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    settings_limits = {
        "auth_login": app_settings.RATE_LIMIT_AUTH, "auth_signup": app_settings.RATE_LIMIT_AUTH,
        "auth_refresh": app_settings.RATE_LIMIT_AUTH,
        "contact_submit": app_settings.RATE_LIMIT_CONTACT,
        "workflow_create": app_settings.RATE_LIMIT_WORKFLOW,
        "integration_build": app_settings.RATE_LIMIT_MCP_BUILD,
        "browser_start": app_settings.RATE_LIMIT_BROWSER_START,
        "resume_generate": app_settings.RATE_LIMIT_RESUME,
        "studio_generate": app_settings.RATE_LIMIT_STUDIO,
        "webhook_ingress": app_settings.RATE_LIMIT_WEBHOOK,
        "provider_request": app_settings.RATE_LIMIT_PROVIDER,
        "general_read": app_settings.RATE_LIMIT_GENERAL,
        "event_stream": app_settings.RATE_LIMIT_EVENT_STREAM,
    }
    if not current.disabled:
        return {"group": group, "limit": current.limit, "already_enabled": True}
    restored_limit = settings_limits.get(group, current.limit if not current.disabled else 0)
    restored = AdmissionPolicy(
        group=current.group, limit=restored_limit,
        window_seconds=current.window_seconds, algorithm=current.algorithm,
        identity=current.identity, shared=current.shared,
        on_store_failure=current.on_store_failure,
        http_status_denied=current.http_status_denied,
        policy_version=f"p3.v1+restored",
    ).validate()
    register_policy(restored)
    factory = _get_factory(request)
    await factory.audit_repo.log_event({
        "event_type": "ADMISSION_POLICY_ENABLED", "actor_id": admin.user_id,
        "actor_type": "USER", "resource_id": group,
        "details": {"restored_limit": restored_limit, "previous_policy_version": current.policy_version},
    })
    return {"group": group, "limit": restored_limit, "policy_version": restored.policy_version,
            "audited": True}


@router.get("/audit")
async def list_audit_events(
    request: Request,
    admin = Depends(require_admin),
    limit: int = 100,
    offset: int = 0,
    event_type: str = None,
):
    """Recent audit events across the platform (audit-log surface, audit P2.11)."""
    factory = _get_factory(request)
    lister = getattr(factory.audit_repo, "list_recent", None)
    if not callable(lister):
        raise HTTPException(status_code=501, detail="Audit listing is not available on this storage backend")
    try:
        events = await lister(limit=limit, offset=offset, event_type=event_type)
    except Exception:
        logger.exception("admin audit listing failed")
        raise HTTPException(status_code=500, detail="Could not list audit events")
    return {"events": events, "count": len(events), "limit": limit, "offset": offset}


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: str, request: Request, admin = Depends(require_admin)):
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    factory = _get_factory(request)
    ok = await factory.user_repo.set_user_active(user_id, False) if hasattr(factory.user_repo, "set_user_active") else False
    if not ok:
        raise HTTPException(status_code=400, detail="User repo does not support activation changes")
    await factory.audit_repo.log_event({
        "event_type": "USER_DISABLED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {},
    })
    return {"user_id": user_id, "is_active": False}


@router.post("/users/{user_id}/enable")
async def enable_user(user_id: str, request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    ok = await factory.user_repo.set_user_active(user_id, True) if hasattr(factory.user_repo, "set_user_active") else False
    if not ok:
        raise HTTPException(status_code=400, detail="User repo does not support activation changes")
    await factory.audit_repo.log_event({
        "event_type": "USER_ENABLED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {},
    })
    return {"user_id": user_id, "is_active": True}



@router.delete("/users/{user_id}")
async def delete_any_user(user_id: str, request: Request, admin = Depends(require_admin)):
    """SUPER ADMIN ONLY: permanently delete any account, including admins."""
    _require_super(admin)
    if user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="The sole super admin cannot delete their own account")
    factory = _get_factory(request)
    if not hasattr(factory.user_repo, "delete_user"):
        raise HTTPException(status_code=400, detail="User repo does not support deletion")
    ok = await factory.user_repo.delete_user(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")
    await factory.audit_repo.log_event({
        "event_type": "USER_DELETED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {"deleted_by": "super_admin"},
    })
    return {"user_id": user_id, "deleted": True}


@router.put("/users/{user_id}/limits")
async def set_user_limits(user_id: str, body: dict, request: Request, admin = Depends(require_admin)):
    """SUPER ADMIN ONLY: set a user's token limit (daily cap) and reset window (hours)."""
    _require_super(admin)
    factory = _get_factory(request)
    updates = {}
    if body.get("daily_token_limit") is not None:
        updates["daily_token_limit"] = max(1000, int(body["daily_token_limit"]))
    if body.get("window_hours") is not None:
        updates["window_hours"] = max(1, min(72, int(body["window_hours"])))
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    await factory.settings_repo.update_settings(user_id, updates)
    await factory.audit_repo.log_event({
        "event_type": "USER_LIMITS_CHANGED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": updates,
    })
    return {"user_id": user_id, **updates}


@router.post("/users/{user_id}/role")

async def set_user_role(user_id: str, body: dict, request: Request, admin = Depends(require_admin)):
    """Promote/demote a user's role: admin | user | viewer | guest."""
    role = str((body or {}).get("role") or "").strip().lower()
    if role not in ("admin", "user", "viewer", "guest"):
        raise HTTPException(status_code=400, detail="Role must be admin, user, viewer, or guest")
    if user_id == admin.user_id and role != "admin":
        raise HTTPException(status_code=400, detail="You cannot demote your own account")
    factory = _get_factory(request)
    if role == "super_admin":
        _require_super(admin)
        # Only ONE super admin may exist: demote any previous holder first.
        try:
            for u in await factory.user_repo.list_users():
                if u.get("role") == "super_admin" and (u.get("user_id") or u.get("id")) != user_id:
                    await factory.user_repo.update_user(u.get("user_id") or u.get("id"), {"role": "admin"})
        except Exception:
            pass
    ok = await factory.user_repo.update_user(user_id, {"role": role}) if hasattr(factory.user_repo, "update_user") else False
    if not ok:
        raise HTTPException(status_code=400, detail="Could not update role")
    await factory.audit_repo.log_event({
        "event_type": "USER_ROLE_CHANGED", "actor_id": admin.user_id, "actor_type": "USER",
        "resource_id": user_id, "details": {"new_role": role},
    })
    return {"user_id": user_id, "role": role}


@router.get("/stats")
async def platform_stats(request: Request, admin = Depends(require_admin)):
    factory = _get_factory(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    users = (await factory.user_repo.list_users()) if hasattr(factory.user_repo, "list_users") else []
    total_runs = active_users_5h = 0
    mcps = 0
    try:
        mcps = len(await factory.mcp_repo.list_mcps())
    except Exception:
        pass
    for u in users:
        uid = u.get("user_id") or u.get("id") or ""
        try:
            runs = await factory.workflow_repo.list_runs(uid, limit=200, offset=0)
            total_runs += len(runs)
            if any(str(r.get("created_at") or "") >= cutoff for r in runs):
                active_users_5h += 1
        except Exception:
            continue
    return {
        "users_total": len(users),
        "users_active_5h": active_users_5h,
        "runs_total": total_runs,
        "integrations": mcps,
        "window_hours": WINDOW_HOURS,
    }


@router.get("/operational-health")
async def operational_health(request: Request,
                             user: AuthenticatedUser = Depends(require_admin)):
    """Phase 20 §20.2: operational health for the selected launch scope —
    queue age, stuck leases, uncertain effects, dead letters, and the
    command deadline horizon. Every threshold that pages an operator is
    visible here before it pages."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    from backend.models.admission import epoch_now
    now = epoch_now()
    factory = getattr(request.app.state, "factory", None)
    db = getattr(factory, "db_manager", None) if factory else None
    if db is None:
        raise HTTPException(status_code=503, detail={
            "code": "HEALTH_UNAVAILABLE",
            "message": "Operational health needs the durable stores"})
    health: dict = {"generated_at": now}

    async def _one(name: str, sql: str, params: tuple = ()) -> None:
        try:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            health[name] = dict(row) if row else {}
        except Exception as exc:
            health[name] = {"error": type(exc).__name__}

    # Queue age: oldest pending command (§20.2).
    await _one("queue", """
        SELECT COUNT(*) AS pending,
               MIN(created_at) AS oldest_created_at,
               ? - MIN(created_at) AS oldest_age_seconds
        FROM pending_commands WHERE status IN ('pending','dispatched')""", (now,))
    # Stuck leases: capacity leases past expiry but still ACTIVE.
    await _one("stuck_leases", """
        SELECT COUNT(*) AS active_expired FROM capacity_leases
        WHERE state='active' AND expires_at<=?""", (now,))
    # Uncertain effects awaiting reconciliation (§5/§20.2).
    await _one("uncertain_effects", """
        SELECT COUNT(*) AS unresolved FROM effects
        WHERE state IN ('DISPATCHING','PROVIDER_PENDING','OUTCOME_UNKNOWN')""")
    # Dead letters / outbox backpressure.
    await _one("outbox", """
        SELECT COUNT(*) AS unpublished,
               MAX(publish_attempts) AS max_publish_attempts
        FROM outbox_events WHERE published_at IS NULL""")
    # Commands past their deadline (the reconciler should expire these).
    await _one("commands_overdue", """
        SELECT COUNT(*) AS overdue FROM commands
        WHERE state IN ('ACCEPTED','QUEUED','RUNNING','WAITING')
        AND deadline_at IS NOT NULL AND deadline_at<=?""", (now,))
    # Recovery episodes with expired leases (§12.5).
    await _one("recovery_stale", """
        SELECT COUNT(*) AS expired_leases FROM recovery_episodes
        WHERE state NOT IN ('VERIFIED_RECOVERY','RECOVERY_FAILED',
                            'ESCALATED','CANCELLED','SUPERSEDED')
        AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""", (now,))
    # Browser sessions past any timer (§8.10).
    await _one("browser_sessions", """
        SELECT COUNT(*) AS sweep_candidates FROM browser_sessions
        WHERE resource_state IN ('READY','RECOVERING','ALLOCATING')
        AND (idle_expires_at<=? OR (handoff_expires_at>0 AND handoff_expires_at<=?)
             OR (absolute_expires_at>0 AND absolute_expires_at<=?))""",
        (now, now, now))
    # Evaluation log coverage (Phase 14 feedback join).
    try:
        routing = getattr(factory, "routing_repo", None)
        if routing is not None and hasattr(routing, "logging_health"):
            health["routing_log"] = await routing.logging_health()
    except Exception:
        health["routing_log"] = {"error": "unavailable"}
    return health
