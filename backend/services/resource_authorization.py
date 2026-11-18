"""Centralized resource authorization (Phase 1.2).

One service answers the only authorization question that matters:
**Can this current actor perform this action on this particular resource
in its current state?** Authentication (a valid JWT) answers only "who";
membership, role, and resource state are resolved here, per request.

Organization routes consult this service instead of inventing local
rules; worker/tool paths use the same primitives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.security.permission_matrix import OrgRole, org_role_allows


class AuthorizationDenied(Exception):
    """Raised by require_membership/require_permission/require_resource_owner.
    Carries a machine-readable reason ('not_a_member' | 'insufficient_role' |
    'not_the_resource_owner') and a user-safe message."""


@dataclass
class MembershipContext:
    """Resolved current membership state for one actor in one org."""
    org_id: str
    org: Dict[str, Any]
    user_id: str
    role: Optional[str]


class ResourceAuthorizationService:
    def __init__(self, org_repo):
        self.org_repo = org_repo

    async def resolve_membership(self, org_id: str, user_id: str) -> MembershipContext:
        """Load the org and the actor's CURRENT role. Non-members get
        role=None (routes translate that to 404 — no existence leak)."""
        org = await self.org_repo.get_org(org_id)
        role = await self.org_repo.get_member_role(org_id, user_id) if org else None
        return MembershipContext(org_id=org_id, org=org or {}, user_id=user_id, role=role)

    async def require_membership(self, org_id: str, user_id: str,
                                 minimum_role: str) -> MembershipContext:
        """Resolve membership and enforce the minimum role. Non-members raise
        a 404-shaped error (handled by callers); insufficient role 403."""
        from backend.security.permission_matrix import _ROLE_LEVEL, OrgRole

        ctx = await self.resolve_membership(org_id, user_id)
        if not ctx.org or ctx.role is None:
            raise AuthorizationDenied("not_a_member")
        minimum = OrgRole(minimum_role)
        if _ROLE_LEVEL[OrgRole(ctx.role)] < _ROLE_LEVEL[minimum]:
            raise AuthorizationDenied(f"requires the {minimum_role} role in this organization")
        return ctx

    async def require_permission(self, org_id: str, user_id: str, action: str) -> MembershipContext:
        """Named-action authorization from the Phase-1.4 matrix, evaluated
        against the actor's CURRENT role (never a cached/claim role)."""
        ctx = await self.resolve_membership(org_id, user_id)
        if not ctx.org or ctx.role is None:
            raise PermissionError("not_a_member")
        if not org_role_allows(ctx.role, action):
            raise AuthorizationDenied(f"requires permission '{action}' in this organization")
        return ctx

    async def require_resource_owner(self, owner_id: str, user_id: str,
                                     is_platform_admin: bool = False) -> None:
        """Personal-resource check: only the owner (or an explicitly defined,
        audited platform admin) may touch it."""
        if owner_id == user_id:
            return
        if is_platform_admin:
            return
        raise AuthorizationDenied("not_the_resource_owner")


_service: Optional[ResourceAuthorizationService] = None


def set_resource_authorization_service(service: ResourceAuthorizationService) -> None:
    global _service
    _service = service


def get_resource_authorization_service() -> Optional[ResourceAuthorizationService]:
    return _service
