"""Agent authority (Phase 15 §15.4/§15.5).

- §15.4: an agent's NAME or prompt-declared role grants NOTHING.
  Effective authority is the INTERSECTION of the user's current
  permission, the parent task's scope, the specialist role's permitted
  operations, and the selected tool's execution policy — resolved from
  the authenticated workflow context and the explicit delegation grant.
- §15.5: authorization is RECHECKED at execution and after resume —
  permissions may change while an agent waits for credentials,
  approval, capacity, or a dependency. Access loss is a DISTINCT
  outcome (never a transient retry), and cached protected content is
  dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

from backend.models.agent_task import AgentRole, ROLE_ALLOWED_TOOLS


@dataclass
class EffectiveAuthority:
    role: AgentRole
    tools: FrozenSet[str]                 # the intersected set
    requires_approval_for: FrozenSet[str] = frozenset()
    denied: List[str] = field(default_factory=list)   # why each drop happened

    def allows(self, tool: str) -> bool:
        return tool in self.tools


def effective_authority(*, role: AgentRole,
                        parent_allowed_tools: List[str],
                        user_permissions: List[str],
                        tool_execution_policy: Optional[Dict[str, str]] = None,
                        pending_approvals: Optional[List[str]] = None,
                        ) -> EffectiveAuthority:
    """§15.4: authority = role whitelist ∩ parent scope ∩ user
    permission ∩ tool execution policy. A tool needing an approval that
    is not currently granted stays OUT of the effective set (the
    specialist must request it; it is never implied). The PROMPT cannot
    add anything — this function takes no agent-claimed input."""
    role_tools = set(ROLE_ALLOWED_TOOLS.get(role, set()))
    parent = set(parent_allowed_tools or [])
    user = set(user_permissions or [])
    denied: List[str] = []
    effective: set = set()
    for tool in sorted(role_tools):
        if tool not in parent:
            denied.append(f"{tool}: outside the parent task's scope")
            continue
        if tool not in user:
            denied.append(f"{tool}: the user's current permission does "
                          "not include it")
            continue
        policy = (tool_execution_policy or {}).get(tool)
        if policy == "denied":
            denied.append(f"{tool}: the tool's execution policy denies it")
            continue
        if policy == "approval_required" and \
                tool not in set(pending_approvals or []):
            denied.append(f"{tool}: requires an approval that is not "
                          "granted")
            continue
        effective.add(tool)
    requires = frozenset(
        t for t, p in (tool_execution_policy or {}).items()
        if p == "approval_required")
    return EffectiveAuthority(role=role, tools=frozenset(effective),
                              requires_approval_for=requires,
                              denied=denied)


class AuthorizationLost(Exception):
    """§15.5: a DISTINCT outcome — the principal lost membership,
    account status, tool access, or credential scope while the work was
    queued/waiting. Never retried as a transient error; cached
    protected content must be dropped."""

    def __init__(self, message: str, *, reason: str,
                 drop_cached: bool = True):
        super().__init__(message)
        self.reason = reason
        self.drop_cached = drop_cached


# §15.5 (finding A): the production authorization provider — set at app
# startup so the runners' recheck consults REAL account/membership state
# when no explicit context is supplied. A provider that cannot answer
# fails OPEN ONLY for "unconfigured"; a configured provider that answers
# with a loss raises the typed AuthorizationLost.
_provider = None


def set_authorization_provider(fn) -> None:
    """fn(principal_id) -> {user_active, membership_current,
    credential_scope_valid, tool_access_current}."""
    global _provider
    _provider = fn


def get_authorization_provider():
    return _provider


# §27.2 (audit 2026-11-13): a run ID identifies WORK, not an actor. A
# protected delegation whose caller supplies no authenticated principal
# must DERIVE the actor from the parent's persisted owner — and fail
# CLOSED when neither exists. The production provider is set at factory
# startup next to the account-authorization provider.
_parent_owner_provider = None


def set_parent_owner_provider(fn) -> None:
    """fn(parent_kind, parent_id) -> owner principal id | None."""
    global _parent_owner_provider
    _parent_owner_provider = fn


async def resolve_principal_from_parent(parent_kind: str,
                                        parent_id: str) -> Optional[str]:
    """The persisted owner of the parent work — the only honest actor
    derivation when the caller has no authenticated principal."""
    if _parent_owner_provider is None:
        return None
    try:
        return await _parent_owner_provider(parent_kind, parent_id)
    except Exception:   # a lookup failure must NOT invent an actor
        return None


async def resolve_authorization_context(principal_id: str)         -> Optional[Dict[str, bool]]:
    """The default context for a recheck: the production provider when
    configured, None otherwise (the deterministic admission gates then
    own the decision — the recheck is not skipped silently)."""
    if _provider is None:
        return None
    return await _provider(principal_id)


async def require_specialist_access(principal_id: str, scope=None, org_id: Optional[str] = None):
    """Require both the parent grant and the current account before using or publishing evidence."""
    current = await resolve_authorization_context(principal_id)
    if get_authorization_provider() is not None and current is None:
        raise AuthorizationLost("Account lookup unavailable", reason="account_unavailable")
    for context in (scope, current):
        if context is not None:
            recheck_authorization(user_active=bool(context.get("user_active", True)),
                                  membership_current=bool(context.get("membership_current", True)),
                                  credential_scope_valid=bool(context.get("credential_scope_valid", True)),
                                  tool_access_current=bool(context.get("tool_access_current", True)))
    if org_id:
        from backend.services.resource_authorization import get_resource_authorization_service
        service = get_resource_authorization_service()
        member = await service.resolve_membership(org_id, principal_id) if service else None
        if member is None or not member.role:
            raise AuthorizationLost("Workflow membership unavailable", reason="membership_revoked")


def recheck_authorization(*, user_active: bool,
                          membership_current: bool,
                          credential_scope_valid: bool,
                          tool_access_current: bool,
                          ) -> None:
    """§15.5: revalidate BEFORE execution and AFTER resume. Raises
    AuthorizationLost (typed) on any loss — the caller stops the
    affected work and drops cached protected data instead of treating
    revocation as transient."""
    if not user_active:
        raise AuthorizationLost(
            "the account is deactivated", reason="account_deactivated")
    if not membership_current:
        raise AuthorizationLost(
            "the workflow membership was revoked", reason="membership_revoked")
    if not tool_access_current:
        raise AuthorizationLost(
            "tool access was revoked for this principal",
            reason="tool_access_revoked")
    if not credential_scope_valid:
        raise AuthorizationLost(
            "the credential's scope no longer covers this operation",
            reason="credential_scope_invalid")
