"""
AgentOS — Auth Dependencies (PRODUCTION)

RS256 JWT verification. No hardcoded tokens. No "test_token" bypass.
User identity comes from authenticated JWT, NEVER from request body.

Usage in routers:
    @router.get("/me")
    async def get_me(user: AuthenticatedUser = Depends(get_current_user)):
        return user
"""

import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

from fastapi import Request, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

from backend.security.jwt_manager import jwt_manager
from backend.security.rbac import Role

logger = logging.getLogger(__name__)

# ── Security scheme ───────────────────────────────────────────────
# auto_error=False so we can provide better error messages
security_scheme = HTTPBearer(auto_error=False)


# ── Authenticated User ───────────────────────────────────────────
@dataclass
class AuthenticatedUser:
    """
    Current account identity resolved after credential verification.
    """
    user_id: str
    email: str
    name: str
    role: str
    auth_provider: str
    # §4.14: CURRENT account authority travels with the principal so
    # decision-time rechecks (approvals, resumes) can verify it.
    is_active: bool = True

    def is_admin(self) -> bool:
        return self.role in (Role.ADMIN, Role.SUPER_ADMIN)

    def is_viewer(self) -> bool:
        return self.role == Role.VIEWER


# ── Dependencies ──────────────────────────────────────────────────

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> AuthenticatedUser:
    """
    Verify the JWT access token and return the authenticated user.

    Validates:
    - Token is present
    - RS256 signature is valid (using public key)
    - Token is not expired
    - Issuer and audience match
    - Token type is 'access'
    - Token jti is not on the server-side logout denylist

    Returns AuthenticatedUser with current account status and role.
    Raises 401 on any failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Provide a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Programmatic access: `vry_` API keys authenticate like JWTs (same
    # role/scoping, resolved from the stored hash, individually revocable).
    # This is the stable alternative to 15-minute JWTs for MCP clients and
    # automations (audit §6: automations had to re-auth constantly).
    if token.startswith("vry_"):
        return await _authenticate_api_key(request, token)

    try:
        claims = jwt_manager.verify_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Use /api/v1/auth/refresh to get a new one.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        logger.warning(f"JWT verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or malformed token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Server-side revocation: a logged-out token must not keep working
    # until its 15-minute TTL runs out.
    jti = claims.get("jti")
    factory = getattr(request.app.state, "factory", None)
    if jti:
        denylist = getattr(factory, "token_denylist_repo", None)
        if denylist is None:
            raise HTTPException(status_code=503, detail="Authentication temporarily unavailable")
        if denylist is not None:
            try:
                if await denylist.is_revoked(jti):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Token has been revoked. Please sign in again.",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            except HTTPException:
                raise
            except Exception:
                logger.exception("Token denylist check failed")
                raise HTTPException(status_code=503, detail="Authentication temporarily unavailable")

    # Extract user identity from verified claims
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject claim.",
        )

    user_repo = getattr(factory, "user_repo", None)
    if user_repo is None:
        raise HTTPException(status_code=503, detail="Authentication temporarily unavailable")
    try:
        account = await user_repo.get_by_id(user_id)
    except Exception:
        logger.exception("Current account lookup failed")
        raise HTTPException(status_code=503, detail="Authentication temporarily unavailable")
    if not account or not account.get("is_active", True):
        raise HTTPException(status_code=401, detail="User account unavailable")

    return AuthenticatedUser(
        user_id=user_id,
        email=account.get("email", ""),
        name=account.get("name", ""),
        role=account.get("role", "viewer"),
        auth_provider=account.get("auth_provider", "local"),
    )


async def _authenticate_api_key(request: Request, token: str) -> AuthenticatedUser:
    """Resolve a `vry_` API key to its user. The hash is the only stored form."""
    import hashlib

    factory = getattr(request.app.state, "factory", None)
    keys_repo = getattr(factory, "api_keys_repo", None) if factory else None
    if keys_repo is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key authentication is not available on this deployment.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    resolved = await keys_repo.resolve_key(key_hash)
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_repo = getattr(factory, "user_repo", None)
    user_data = await user_repo.get_by_id(resolved["user_id"]) if user_repo else None
    if not user_data or not user_data.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthenticatedUser(
        user_id=user_data["id"],
        email=user_data.get("email", ""),
        name=user_data.get("name", ""),
        role=user_data.get("role", "user"),
        auth_provider="api_key",
        is_active=bool(user_data.get("is_active", True)),
    )


async def get_current_user_id(
    user: AuthenticatedUser = Depends(get_current_user),
) -> str:
    """Convenience dependency that returns just the user_id string."""
    return user.user_id


async def require_admin(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require admin role. Returns user if admin, raises 403 otherwise."""
    if not user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


async def require_not_viewer(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Require at least 'user' role (not 'viewer'). For write operations."""
    if user.is_viewer():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Write access required. Viewer role cannot perform this action.",
        )
    return user
