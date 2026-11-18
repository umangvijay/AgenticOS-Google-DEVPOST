"""Route & workload admission policy inventory (Phase 3, §3.3).

ONE authoritative mapping from protected entry point to its limits, identity
scope, algorithm, and failure posture. Every admission call must resolve its
group here; an unknown group raises AdmissionPolicyError instead of silently
borrowing the general allowance (the pre-Phase-3 behavior that made
resume_analyze/resume_tailor inherit an undocumented limit).

Settings supply the numbers; this module supplies the semantics. Changing a
limit is configuration; changing a group's shape (algorithm, failure mode,
identity scope) is a policy-version change.
"""
from __future__ import annotations

from backend.config.settings import settings
from backend.models.admission import (
    AdmissionPolicy,
    AdmissionPolicyError,
    IdentityScope,
    RateAlgorithm,
    StoreFailureMode,
)

# ── Canonical groups (§3.3 inventory) ─────────────────────────────────

_POLICIES: dict[str, AdmissionPolicy] = {
    # Abuse-sensitive public surface: cross-replica shared enforcement, and
    # fail-open to the bounded per-process limiter during a store outage
    # (availability over strictness; the edge is the documented volume backstop).
    "auth_login": AdmissionPolicy(
        group="auth_login", limit=settings.RATE_LIMIT_AUTH,
        identity=IdentityScope.IP, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "auth_signup": AdmissionPolicy(
        group="auth_signup", limit=settings.RATE_LIMIT_AUTH,
        identity=IdentityScope.IP, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "auth_refresh": AdmissionPolicy(
        group="auth_refresh", limit=settings.RATE_LIMIT_AUTH,
        identity=IdentityScope.IP, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "contact_submit": AdmissionPolicy(
        group="contact_submit", limit=settings.RATE_LIMIT_CONTACT,
        identity=IdentityScope.IP, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    # Expensive / capacity-owning work: a store outage must not silently
    # downgrade a strict allowance to N per-process allowances (§3.9), so
    # these fail CLOSED — refusal with ADMISSION_UNAVAILABLE, not free admission.
    "workflow_create": AdmissionPolicy(
        group="workflow_create", limit=settings.RATE_LIMIT_WORKFLOW,
        identity=IdentityScope.USER, shared=True,
        on_store_failure=StoreFailureMode.FAIL_CLOSED,
    ).validate(),
    "integration_build": AdmissionPolicy(
        group="integration_build", limit=settings.RATE_LIMIT_MCP_BUILD,
        identity=IdentityScope.USER, shared=True,
        on_store_failure=StoreFailureMode.FAIL_CLOSED,
    ).validate(),
    "browser_start": AdmissionPolicy(
        group="browser_start", limit=settings.RATE_LIMIT_BROWSER_START,
        identity=IdentityScope.USER, shared=True,
        on_store_failure=StoreFailureMode.FAIL_CLOSED,
    ).validate(),
    # Authenticated generation surface: shared, fail-open (a store outage
    # degrades to the per-process cap rather than blocking document work).
    "resume_generate": AdmissionPolicy(
        group="resume_generate", limit=settings.RATE_LIMIT_RESUME,
        identity=IdentityScope.USER, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "studio_generate": AdmissionPolicy(
        group="studio_generate", limit=settings.RATE_LIMIT_STUDIO,
        identity=IdentityScope.USER, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "webhook_ingress": AdmissionPolicy(
        group="webhook_ingress", limit=settings.RATE_LIMIT_WEBHOOK,
        identity=IdentityScope.IP, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    # Worker-side provider throughput against one credential scope. Enforced
    # shared so parallel workers split one allowance rather than each getting one.
    "provider_request": AdmissionPolicy(
        group="provider_request", limit=settings.RATE_LIMIT_PROVIDER,
        identity=IdentityScope.CREDENTIAL, shared=True,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    # High-volume read/observability paths stay in-process BY DESIGN (§3.6):
    # a database round-trip per request would cost more than the limit protects.
    "general_read": AdmissionPolicy(
        group="general_read", limit=settings.RATE_LIMIT_GENERAL,
        identity=IdentityScope.USER, shared=False,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
    "event_stream": AdmissionPolicy(
        group="event_stream", limit=settings.RATE_LIMIT_EVENT_STREAM,
        identity=IdentityScope.USER, shared=False,
        on_store_failure=StoreFailureMode.FAIL_OPEN_LOCAL,
    ).validate(),
}

# ── Legacy aliases (pre-Phase-3 group strings still present in routers) ─
# Each alias resolves to the canonical policy; the canonical group name is
# what gets counted, so an alias and its target share one allowance.
_ALIASES: dict[str, str] = {
    "auth": "auth_login",
    "contact": "contact_submit",
    "webhook": "webhook_ingress",
    "workflow": "workflow_create",
    "mcp_build": "integration_build",
    "general": "general_read",
    "resume_analyze": "resume_generate",
    "resume_tailor": "resume_generate",
    "capabilities": "studio_generate",
}


def resolve_policy(group: str) -> AdmissionPolicy:
    """Resolve a route's group to its policy. Unknown groups are a
    configuration ERROR — never an implicit fallthrough to `general_read`."""
    canonical = _ALIASES.get(group, group)
    policy = _POLICIES.get(canonical)
    if policy is None:
        raise AdmissionPolicyError(
            f"Unknown admission group {group!r}; register it in "
            f"backend/security/admission_policy.py before enforcing it", group)
    return policy


def register_policy(policy: AdmissionPolicy) -> None:
    """Runtime registration for tests and operator overrides. Deliberately
    not part of the request path."""
    _POLICIES[policy.validate().group] = policy


def all_policies() -> dict[str, AdmissionPolicy]:
    """The full inventory (for the admin introspection endpoint and docs)."""
    return dict(_POLICIES)


# Compatibility: the pre-Phase-3 module-level limit map, derived from the
# registry so legacy imports observe the same numbers.
RATE_LIMIT_GROUPS: dict[str, int] = {
    alias: resolve_policy(alias).limit for alias in _ALIASES
}
