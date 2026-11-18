"""Phase 1.4 — one named role-and-permission matrix.

Every authorization decision in the organization domain consults this
matrix; routes never invent their own rules, and frontend visibility is
never the security control. Platform roles (user/admin) are distinct from
organization roles (owner/admin/member/viewer): an org owner is NOT a
platform administrator.
"""
from __future__ import annotations

from enum import Enum


class OrgRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class PlatformRole(str, Enum):
    USER = "user"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"
    VIEWER = "viewer"


# Named actions (Phase 1.4). Each maps to the minimum organization role
# that may perform it; None means the action is owner-only.
ORG_PERMISSIONS: Dict[str, Optional[OrgRole]] = {
    "organization.read": OrgRole.VIEWER,
    "organization.invite_member": OrgRole.ADMIN,
    "organization.invite_admin": OrgRole.OWNER,          # owner-only
    "organization.manage_roles": OrgRole.OWNER,          # owner-only role changes
    "organization.remove_member": OrgRole.ADMIN,
    "organization.remove_admin": OrgRole.OWNER,          # only the owner removes admins
    "organization.delete": OrgRole.OWNER,
    "organization.oversight": OrgRole.ADMIN,
    "organization.transfer_ownership": None,             # dedicated owner flow only
    "credential.use": OrgRole.MEMBER,                    # members may USE org credentials
    "credential.manage": OrgRole.ADMIN,
    "artifact.download": OrgRole.VIEWER,
    "workflow.create": OrgRole.MEMBER,
    "workflow.read": OrgRole.VIEWER,
}

# Phase 30.6 — named OPERATOR actions (platform-level; the operator acts
# on the deployment, not as an org member). Each maps to the minimum
# platform role. Decrypt, key administration and policy approval are
# deliberately SEPARATE permissions: holding one never implies another,
# and an org owner is never an operator by that fact alone.
OPERATOR_ACTIONS: Dict[str, Optional[PlatformRole]] = {
    "ops.incident.inspect": PlatformRole.ADMIN,
    "ops.job.cancel": PlatformRole.ADMIN,
    "ops.effect.reconcile": PlatformRole.ADMIN,
    "ops.credential.rotate": PlatformRole.ADMIN,
    "ops.credential.decrypt": PlatformRole.ADMIN,     # separate from key administration
    "ops.key.administer": PlatformRole.SUPER_ADMIN,   # wrapping-key rotation only
    "ops.policy.approve": PlatformRole.ADMIN,         # separate from key administration
    "ops.backup.restore": PlatformRole.SUPER_ADMIN,
}

_ROLE_LEVEL = {OrgRole.OWNER: 4, OrgRole.ADMIN: 3, OrgRole.MEMBER: 2, OrgRole.VIEWER: 1}

_PLATFORM_LEVEL = {
    PlatformRole.VIEWER: 0,
    PlatformRole.USER: 1,
    PlatformRole.ADMIN: 2,
    PlatformRole.SUPER_ADMIN: 3,
}


def operator_role_allows(platform_role: Optional[str], action: str) -> bool:
    """True when the platform role may perform the named operator action.
    Unknown actions default to DENY."""
    minimum = OPERATOR_ACTIONS.get(action)
    if minimum is None:
        return False
    try:
        level = _PLATFORM_LEVEL[PlatformRole(platform_role)]
    except (ValueError, KeyError):
        return False
    return level >= _PLATFORM_LEVEL[minimum]


def org_role_allows(role: Optional[str], action: str) -> bool:
    """True when the organization role may perform the named action.
    Unknown actions default to DENY."""
    minimum = ORG_PERMISSIONS.get(action)
    if minimum is None:
        return action in ORG_PERMISSIONS and ORG_PERMISSIONS[action] is None and role == OrgRole.OWNER \
            if action in ORG_PERMISSIONS else False
    try:
        level = _ROLE_LEVEL[OrgRole(role)]
    except (ValueError, KeyError):
        return False
    return level >= _ROLE_LEVEL[minimum]


def platform_is_admin(platform_role: Optional[str]) -> bool:
    """Platform-administrator exceptional access — explicitly defined and
    audited at call sites. An org owner is NOT a platform admin."""
    return platform_role in (PlatformRole.ADMIN, PlatformRole.SUPER_ADMIN)
