"""PostgreSQL repository inventory (§21.3).

Every repository the application requires on the Postgres backend:
users, sessions (refresh tokens + denylist), organizations, credentials
(secrets + api keys), tools (MCP registry + builds), commands + outbox +
approvals, effects, browser ownership, recovery episodes, evaluations,
memory (with DURABLE dependencies — §21.19), deliverables + publication
jobs, feedback (§21.21), notifications, settings, audit, idempotency,
schedules, experiments, execution steps, usage.

Method contracts mirror the SQLite implementations so the factory wires
the same accessors and the same services run on both backends.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.repositories.postgres.database import PostgresDatabaseManager
from backend.repositories.postgres.browser_contract import PostgresBrowserContract


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO string → aware datetime for timestamptz params."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _epoch_to_ts(epoch: Optional[int]) -> Optional[datetime]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _ts_to_epoch(ts: Optional[datetime]) -> Optional[int]:
    return int(ts.timestamp()) if ts else None


class _Base:
    def __init__(self, db: PostgresDatabaseManager):
        self.db = db


# ═══════════════════════════════════════════════════════════════════════
# Users + sessions
# ═══════════════════════════════════════════════════════════════════════

class PostgresUserRepository(_Base):
    async def create_user(self, user: Dict[str, Any]) -> Dict[str, Any]:
        await self.db.execute(
            """INSERT INTO pg_users (id, email, name, password_hash,
                 auth_provider, google_id, avatar_url, role)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (id) DO NOTHING""",
            user.get("id") or f"user-{uuid.uuid4().hex[:12]}",
            user["email"], user.get("name", ""),
            user.get("password_hash"), user.get("auth_provider", "local"),
            user.get("google_id"), user.get("avatar_url"),
            user.get("role", "user"))
        return await self.get_by_email(user["email"])

    async def get_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM pg_users WHERE id=$1",
                                      user_id)
        return self._shape(row) if row else None

    async def get_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM pg_users WHERE email=$1",
                                      email)
        return self._shape(row) if row else None

    async def get_by_google_id(self, google_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_users WHERE google_id=$1", google_id)
        return self._shape(row) if row else None

    @staticmethod
    def _coerce_timestamp(value: Any) -> Any:
        """asyncpg binds only real datetime objects — callers (and the
        SQLite repo) legitimately pass ISO strings for last_login and
        locked_until. An unparseable string passes through so the
        database still rejects it explicitly."""
        if isinstance(value, str):
            try:
                from datetime import datetime as _dt
                return _dt.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        return value

    async def update_user(self, user_id: str, updates: Dict[str, Any]) -> None:
        cols, args, idx = [], [], 1
        for key in ("name", "avatar_url", "role", "password_hash",
                    "last_login", "locked_until"):
            if key in updates:
                cols.append(f"{key}=${idx}")
                value = updates[key]
                if key in ("last_login", "locked_until"):
                    value = self._coerce_timestamp(value)
                args.append(value)
                idx += 1
        if not cols:
            return
        args.append(user_id)
        await self.db.execute(
            f"UPDATE pg_users SET {', '.join(cols)}, updated_at=now() "
            f"WHERE id=${idx}", *args)

    async def increment_failed_logins(self, user_id: str) -> int:
        row = await self.db.fetch_one(
            "UPDATE pg_users SET failed_login_attempts=failed_login_attempts+1 "
            "WHERE id=$1 RETURNING failed_login_attempts", user_id)
        return int(row["failed_login_attempts"]) if row else 0

    async def reset_failed_logins(self, user_id: str) -> None:
        await self.db.execute(
            "UPDATE pg_users SET failed_login_attempts=0 WHERE id=$1", user_id)

    async def set_lockout(self, user_id: str, until: Any) -> None:
        await self.db.execute(
            "UPDATE pg_users SET locked_until=$2 WHERE id=$1",
            user_id, self._coerce_timestamp(until))

    async def set_user_active(self, user_id: str, active: bool) -> None:
        await self.db.execute(
            "UPDATE pg_users SET is_active=$2 WHERE id=$1", user_id, active)

    async def list_users(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_users ORDER BY created_at LIMIT $1 OFFSET $2",
            limit, offset)
        return [self._shape(r) for r in rows]

    async def delete_user(self, user_id: str) -> None:
        await self.db.execute("DELETE FROM pg_users WHERE id=$1", user_id)

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        for ts_key in ("locked_until", "last_login", "created_at", "updated_at"):
            if out.get(ts_key) is not None:
                out[ts_key] = out[ts_key].isoformat()
        return out


class PostgresRefreshTokenRepository(_Base):
    async def store_token(self, user_id: str, token_hash: str,
                          expires_at: Any, token_id: str = "") -> str:
        tid = token_id or f"rt-{uuid.uuid4().hex[:16]}"
        exp = expires_at if isinstance(expires_at, datetime) \
            else _epoch_to_ts(int(expires_at))
        await self.db.execute(
            """INSERT INTO pg_refresh_tokens (token_id, user_id, token_hash,
                 expires_at) VALUES ($1,$2,$3,$4)
               ON CONFLICT (token_id) DO NOTHING""",
            tid, user_id, token_hash, exp)
        return tid

    async def validate_and_consume(self, token_hash: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """UPDATE pg_refresh_tokens
               SET consumed=TRUE
               WHERE token_hash=$1 AND revoked=FALSE AND consumed=FALSE
                 AND expires_at > now()
               RETURNING token_id, user_id""",
            token_hash)
        return dict(row) if row else None

    async def revoke_all_for_user(self, user_id: str) -> int:
        row = await self.db.fetch_one(
            "UPDATE pg_refresh_tokens SET revoked=TRUE "
            "WHERE user_id=$1 AND revoked=FALSE RETURNING token_id", user_id)
        return 1 if row else 0

    async def cleanup_expired(self) -> int:
        row = await self.db.fetch_one(
            "WITH d AS (DELETE FROM pg_refresh_tokens WHERE expires_at < now() "
            "OR (consumed AND created_at < now() - interval '1 day') "
            "RETURNING 1) SELECT count(*) AS n FROM d")
        return int(row["n"]) if row else 0


class PostgresTokenDenylistRepository(_Base):
    async def is_revoked(self, jti: str) -> bool:
        return await self.is_denied(jti)

    async def revoke_jti(self, jti: str, expires_at: Any) -> None:
        """Phase 22 (found live): logout calls revoke_jti on every backend
        contract — the pg profile was missing it, so logged-out access
        tokens kept working until their TTL. Parity with
        SQLiteTokenDenylistRepository.revoke_jti (empty jti is a no-op)."""
        if not jti:
            return
        await self.add(jti, expires_at)

    async def add(self, jti: str, expires_at: Any) -> None:
        exp = expires_at if isinstance(expires_at, datetime) \
            else _epoch_to_ts(int(expires_at))
        await self.db.execute(
            """INSERT INTO pg_token_denylist (jti, expires_at) VALUES ($1,$2)
               ON CONFLICT (jti) DO NOTHING""", jti, exp)

    async def is_denied(self, jti: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT 1 AS x FROM pg_token_denylist WHERE jti=$1", jti)
        return row is not None


# ═══════════════════════════════════════════════════════════════════════
# Organizations
# ═══════════════════════════════════════════════════════════════════════

class PostgresOrgRepository(_Base):
    async def create_org(self, name: str, owner_id: str) -> Dict[str, Any]:
        """Production contract parity (SQLiteOrgRepository.create_org):
        (name, owner_id) → generates the id, adds the owner as 'owner'
        member, returns an org_id-keyed dict. The Phase 22 live probe
        caught the pg divergence (extra org_id arg, id-keyed return) as a
        500 on POST /api/v1/orgs."""
        org_id = str(uuid.uuid4())
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO pg_orgs (id, name, owner_user_id) "
                    "VALUES ($1,$2,$3)", org_id, name, owner_id)
                await conn.execute(
                    "INSERT INTO pg_org_members (org_id, user_id, role) "
                    "VALUES ($1,$2,'owner')", org_id, owner_id)
        return {"org_id": org_id, "name": name,
                "owner_user_id": owner_id}

    async def get_org(self, org_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM pg_orgs WHERE id=$1",
                                      org_id)
        return dict(row) if row else None

    async def list_orgs_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            """SELECT o.* FROM pg_orgs o JOIN pg_org_members m
               ON m.org_id = o.id WHERE m.user_id=$1""", user_id)

    async def get_member_role(self, org_id: str, user_id: str) -> Optional[str]:
        row = await self.db.fetch_one(
            "SELECT role FROM pg_org_members WHERE org_id=$1 AND user_id=$2",
            org_id, user_id)
        return row["role"] if row else None

    async def list_members(self, org_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT user_id, role, created_at FROM pg_org_members "
            "WHERE org_id=$1 ORDER BY created_at", org_id)

    async def add_member(self, org_id: str, user_id: str, role: str) -> None:
        await self.db.execute(
            """INSERT INTO pg_org_members (org_id, user_id, role)
               VALUES ($1,$2,$3) ON CONFLICT (org_id, user_id)
               DO UPDATE SET role=EXCLUDED.role""", org_id, user_id, role)

    async def set_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_org_members SET role=$3 "
            "WHERE org_id=$1 AND user_id=$2 RETURNING user_id",
            org_id, user_id, role)
        return row is not None

    async def transfer_ownership(self, org_id: str, from_user: str,
                                 to_user: str) -> bool:
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE pg_org_members SET role='owner' "
                    "WHERE org_id=$1 AND user_id=$2 RETURNING user_id",
                    org_id, to_user)
                if row is None:
                    return False
                await conn.execute(
                    "UPDATE pg_org_members SET role='admin' "
                    "WHERE org_id=$1 AND user_id=$2", org_id, from_user)
                await conn.execute(
                    "UPDATE pg_orgs SET owner_user_id=$2 WHERE id=$1",
                    org_id, to_user)
        return True

    async def create_invitation(self, org_id: str, email: str, role: str,
                                invited_by: str, token_hash: str = "",
                                expires_at: str = "") -> Optional[Dict[str, Any]]:
        """Production contract parity (shared invitation service +
        SQLiteOrgRepository): store ONLY the token hash, with an expiry;
        a duplicate PENDING invitation for the same email returns None
        (the router maps that to 409)."""
        inv_id = f"inv-{uuid.uuid4().hex[:12]}"
        if not token_hash:
            token_hash = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
        # Contract: a duplicate PENDING invitation for the same email → None
        # (router maps to 409). Real storage failures propagate as errors,
        # never masquerade as a duplicate.
        dup = await self.db.fetch_one(
            "SELECT 1 AS x FROM pg_org_invitations "
            "WHERE org_id=$1 AND lower(email)=lower($2) AND status='pending'",
            org_id, email)
        if dup:
            return None
        # asyncpg needs a real datetime for timestamptz params (the shared
        # service hands over ISO strings).
        exp_dt = _parse_ts(expires_at) if expires_at else None
        await self.db.execute(
            """INSERT INTO pg_org_invitations (id, org_id, email, role,
                 invited_by, token, token_hash, expires_at, status)
               VALUES ($1,$2,$3,$4,$5,$6,$7,
                       COALESCE($8::timestamptz, now() + interval
                                '30 days'), 'pending')""",
            inv_id, org_id, email, role, invited_by,
            # The legacy `token` column keeps its NOT NULL/UNIQUE contract;
            # from the hash-only migration onward it stores the hash — a
            # raw invitation token is never persisted.
            token_hash, token_hash, exp_dt)
        row = await self.db.fetch_one(
            "SELECT * FROM pg_org_invitations WHERE id=$1", inv_id)
        out = dict(row) if row else None
        if out:
            out["invitation_id"] = out["id"]
        return out

    async def list_invitations(self, org_id: str,
                               status: str = "pending") -> List[Dict[str, Any]]:
        """Production contract parity: (org_id, status filter). Tokens are
        never listable — only existence, role, and expiry."""
        return await self.db.fetch_all(
            """SELECT id AS invitation_id, org_id, email, role, invited_by,
                      status, created_at, expires_at
               FROM pg_org_invitations WHERE org_id=$1 AND status=$2
               ORDER BY created_at DESC""", org_id, status)

    async def accept_invitation(self, token_hash: str, user_id: str,
                                email: str = "") -> Dict[str, Any]:
        """Production contract parity (shared invitation service +
        SQLiteOrgRepository.accept_invitation): the TOKEN HASH (never the
        raw token), email match, expiry, and atomic membership creation.
        Raises ValueError with the reason; returns the membership row."""
        if not token_hash:
            raise ValueError("invitation token is required")
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                inv = await conn.fetchrow(
                    "SELECT * FROM pg_org_invitations "
                    "WHERE token_hash=$1 AND status='pending' FOR UPDATE",
                    token_hash)
                if inv is None:
                    raise ValueError("invitation not found or not pending")
                if inv["expires_at"] and \
                        inv["expires_at"] < datetime.now(timezone.utc):
                    raise ValueError("invitation has expired")
                if email and inv["email"].lower() != email.lower():
                    raise ValueError("invitation was issued to a different email")
                await conn.execute(
                    "UPDATE pg_org_invitations SET status='accepted', "
                    "updated_at=now() WHERE id=$1", inv["id"])
                await conn.execute(
                    """INSERT INTO pg_org_members (org_id, user_id, role)
                       VALUES ($1,$2,$3)
                       ON CONFLICT (org_id, user_id) DO NOTHING""",
                    inv["org_id"], user_id, inv["role"])
                return {"org_id": inv["org_id"], "user_id": user_id,
                        "role": inv["role"]}

    async def resend_invitation(self, org_id: str, invitation_id: str,
                                invited_by: str, new_token_hash: str,
                                new_expires_at: str) -> Optional[Dict[str, Any]]:
        """Production contract parity (Phase 1.5 resend policy): rotates
        the token hash, extends the expiry, PRESERVES the role, and only
        pending invitations can be resent. Returns the updated row."""
        row = await self.db.fetch_one(
            """UPDATE pg_org_invitations
                 SET token=$2, token_hash=$2, expires_at=$3,
                     invited_by=$4, status='pending', updated_at=now()
               WHERE id=$1 AND org_id=$5 AND status='pending'
               RETURNING id, org_id, email, role, invited_by, status,
                         expires_at""",
            invitation_id, new_token_hash,
            _parse_ts(new_expires_at) if new_expires_at else None,
            invited_by, org_id)
        out = dict(row) if row else None
        if out:
            out["invitation_id"] = out["id"]
        return out

    async def get_invitation_by_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        """Production contract parity: lookup by TOKEN HASH (the raw token
        is never stored)."""
        row = await self.db.fetch_one(
            "SELECT * FROM pg_org_invitations WHERE token_hash=$1",
            token_hash)
        return dict(row) if row else None

    async def revoke_invitation(self, invitation_id: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_org_invitations SET status='revoked', updated_at=now() "
            "WHERE id=$1 AND status='pending' RETURNING id", invitation_id)
        return row is not None


# ═══════════════════════════════════════════════════════════════════════
# Credentials: secrets (vault) + api keys
# ═══════════════════════════════════════════════════════════════════════

class PostgresSecretsRepository(_Base):
    async def store_secret(self, user_id: str, key: str,
                           ciphertext: str) -> None:
        await self.db.execute(
            """INSERT INTO pg_secrets (user_id, key, ciphertext, updated_at)
               VALUES ($1,$2,$3,now())
               ON CONFLICT (user_id, key)
               DO UPDATE SET ciphertext=EXCLUDED.ciphertext, updated_at=now()""",
            user_id, key, ciphertext)

    async def get_secret(self, user_id: str, key: str) -> Optional[str]:
        row = await self.db.fetch_one(
            "SELECT ciphertext FROM pg_secrets WHERE user_id=$1 AND key=$2",
            user_id, key)
        return row["ciphertext"] if row else None

    async def delete_secret(self, user_id: str, key: str) -> None:
        await self.db.execute(
            "DELETE FROM pg_secrets WHERE user_id=$1 AND key=$2", user_id, key)

    async def list_secret_keys(self, user_id: str) -> List[str]:
        rows = await self.db.fetch_all(
            "SELECT key FROM pg_secrets WHERE user_id=$1 ORDER BY key", user_id)
        return [r["key"] for r in rows]


class PostgresApiKeysRepository(_Base):
    async def create_key(self, user_id: str, key_hash: str, prefix: str, name: str) -> str:
        row = await self.create(user_id, key_hash, name=name, prefix=prefix)
        return row["id"]

    async def resolve_key(self, key_hash: str) -> Optional[Dict[str, Any]]:
        return await self.db.fetch_one(
            "UPDATE pg_api_keys SET last_used_at=now() WHERE key_hash=$1 AND is_active "
            "RETURNING user_id, id AS key_id", key_hash)

    async def list_keys(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.list_for_user(user_id)

    async def revoke_key(self, user_id: str, key_id: str) -> bool:
        return await self.revoke(key_id, user_id)

    async def create(self, user_id: str, key_hash: str, name: str = "",
                     prefix: str = "") -> Dict[str, Any]:
        key_id = f"ak-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            "INSERT INTO pg_api_keys (id, user_id, name, key_hash, prefix) "
            "VALUES ($1,$2,$3,$4,$5)", key_id, user_id, name, key_hash, prefix)
        return {"id": key_id, "user_id": user_id, "name": name}

    async def find_by_hash(self, key_hash: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_api_keys WHERE key_hash=$1 AND is_active",
            key_hash)
        return dict(row) if row else None

    async def revoke(self, key_id: str, user_id: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_api_keys SET is_active=FALSE, revoked_at=now() "
            "WHERE id=$1 AND user_id=$2 AND is_active RETURNING id", key_id, user_id)
        return row is not None

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT id, name, prefix, is_active, created_at, last_used_at, revoked_at "
            "FROM pg_api_keys WHERE user_id=$1 ORDER BY created_at DESC, id", user_id)


# ═══════════════════════════════════════════════════════════════════════
# Tools: MCP registry + builds
# ═══════════════════════════════════════════════════════════════════════

from backend.repositories.postgres.connector_contract import PostgresConnectorContract


class PostgresMCPRepository(PostgresConnectorContract, _Base):
    """Mirrors SQLiteMCPRepository: mcps + mcp_tools + mcp_builds."""

    async def register_mcp(self, mcp: Dict[str, Any]) -> str:
        mcp_id = mcp.get("id") or mcp.get("mcp_id") or f"mcp-{uuid.uuid4().hex[:12]}"
        # Ownership: the registry payload carries `owner` (SQLite/Firestore
        # contract column). Mirror it into user_id so the pg read filter and
        # cross-repo parity hold (Phase 22: workflow-built tools must be
        # visible to their owner on the postgres profile).
        user_id = mcp.get("user_id") or mcp.get("owner", "")
        registered = await self.db.fetch_one(
            """INSERT INTO pg_mcps (id, user_id, name, artifact_type,
                 trust_tier, spec_provenance, source_spec, base_url,
                 auth_type, credential_ref, owner, version, legacy_metadata, contract_metadata, enabled, state, test_coverage)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15,$16,$17::jsonb)
               ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,
                 trust_tier=EXCLUDED.trust_tier,
                 spec_provenance=EXCLUDED.spec_provenance,
                 test_coverage=EXCLUDED.test_coverage, contract_metadata=EXCLUDED.contract_metadata,
                 base_url=EXCLUDED.base_url, auth_type=EXCLUDED.auth_type,
                 legacy_metadata=EXCLUDED.legacy_metadata, state=EXCLUDED.state,
                 enabled=EXCLUDED.enabled, updated_at=now()
               WHERE pg_mcps.owner=EXCLUDED.owner RETURNING id""",
            mcp_id, user_id, mcp.get("name", ""),
            mcp.get("artifact_type", "adapter"),
            mcp.get("trust_tier", "candidate"),
            mcp.get("spec_provenance", ""), mcp.get("source_spec"),
            # Registry payloads carry `endpoint` (cross-repo contract);
            # accept the legacy base_url spelling too.
            mcp.get("base_url") or mcp.get("endpoint"),
            mcp.get("auth_type") or (mcp.get("auth") or {}).get("type", "NONE"),
            mcp.get("credential_ref"), mcp.get("owner") or user_id,
            mcp.get("version", "v1"), _json(mcp.get("metadata", {})), _json(mcp),
            bool(mcp.get("is_enabled", mcp.get("enabled", False))), mcp.get("state", "registered"),
            _json(mcp.get("test_coverage", {})))
        if registered is None:
            raise PermissionError("Connector belongs to another owner")
        return mcp_id

    async def get_mcp(self, mcp_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM pg_mcps WHERE id=$1",
                                      mcp_id)
        return self._shape(row) if row else None

    async def list_mcps(self, user_id: str) -> List[Dict[str, Any]]:
        # Ownership contract parity with SQLite/Firestore: a user sees the
        # tools they own (either ownership column) plus `system` tools.
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_mcps WHERE user_id=$1 OR owner=$1 "
            "OR owner='system' ORDER BY created_at DESC",
            user_id)
        return [self._shape(r) for r in rows]

    async def update_mcp_health(self, mcp_id: str, status: str, timestamp: Optional[datetime] = None) -> None:
        await self.db.execute(
            "UPDATE pg_mcps SET health_status=$2, updated_at=now() "
            "WHERE id=$1", mcp_id, status)

    async def set_mcp_enabled(self, mcp_id: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE pg_mcps SET enabled=$2, legacy_metadata=legacy_metadata-'pending_publication_token', "
            "updated_at=now() WHERE id=$1",
            mcp_id, enabled)

    async def publish_managed_candidate(self, mcp_id, *, owner, revision_id, publication_token, metadata):
        row = await self.db.fetch_one(
            "UPDATE pg_mcps SET enabled=true, trust_tier='verified', legacy_metadata=$5::jsonb, updated_at=now() "
            "WHERE id=$1 AND owner=$2 AND active_revision_id=$3 AND NOT enabled "
            "AND legacy_metadata->>'pending_publication_token'=$4 RETURNING id",
            mcp_id, owner, revision_id, publication_token, _json(metadata))
        return row is not None

    async def update_mcp_auth(self, mcp_id: str, auth_type: str,
                              credential_ref: str) -> None:
        await self.db.execute(
            "UPDATE pg_mcps SET auth_type=$2, credential_ref=$3, "
            "updated_at=now() WHERE id=$1", mcp_id, auth_type, credential_ref)

    async def update_mcp_state(self, mcp_id: str, state: str) -> None:
        await self.db.execute(
            "UPDATE pg_mcps SET state=$2, updated_at=now() WHERE id=$1",
            mcp_id, state)

    async def set_public(self, mcp_id: str, public: bool) -> None:
        await self.db.execute(
            "UPDATE pg_mcps SET is_public=$2, updated_at=now() WHERE id=$1",
            mcp_id, public)

    async def list_public_mcps(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_mcps WHERE is_public AND enabled "
            "ORDER BY created_at DESC")
        return [self._shape(r) for r in rows]

    async def delete_mcp(self, mcp_id: str) -> None:
        await self.db.execute("DELETE FROM mcp_tools WHERE mcp_id=$1", mcp_id)
        await self.db.execute("DELETE FROM pg_mcps WHERE id=$1", mcp_id)

    async def cache_tools(self, mcp_id: str, tools: List[Dict[str, Any]], *, replace_all: bool = True) -> None:
        """Replace the tool cache for one registry (sqlite parity)."""
        import datetime as _dt
        now = datetime.now(timezone.utc).isoformat()
        expires = (_dt.datetime.now(_dt.timezone.utc)
                   + _dt.timedelta(hours=24)).isoformat()
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                if replace_all:
                    await conn.execute("DELETE FROM mcp_tools WHERE mcp_id=$1", mcp_id)
                else:
                    await conn.execute("DELETE FROM mcp_tools WHERE mcp_id=$1 AND tool_name=ANY($2::text[])",
                                       mcp_id, [tool["tool_name"] for tool in tools])
                for t in tools:
                    op = t.get("operation") or {}
                    await conn.execute(
                        """INSERT INTO mcp_tools (tool_name, description,
                             input_schema, mcp_id, mcp_version, discovered_at,
                             expires_at, auth_requirements, risk_level, operation_json)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)""",
                        t.get("name") or t.get("tool_name", ""),
                        t.get("description", ""),
                        json.dumps(t.get("input_schema")
                                   or t.get("inputSchema") or {}),
                        mcp_id, t.get("mcp_version", "v1"), now, expires,
                        json.dumps(t.get("auth_requirements", [])),
                        int(t.get("risk_level", 4)), _json(op))

    async def get_cached_tools(self, mcp_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if mcp_id:
            rows = await self.db.fetch_all(
                "SELECT * FROM mcp_tools WHERE mcp_id=$1", mcp_id)
        else:
            rows = await self.db.fetch_all("SELECT * FROM mcp_tools")
        results = []
        for row in rows:
            r = dict(row)
            r["input_schema"] = _parse_json(r.get("input_schema") or "{}")
            r["auth_requirements"] = _parse_json(r.get("auth_requirements") or "[]")
            r["operation"] = _parse_json(r.pop("operation_json", None)) or {}
            r["name"] = r.get("tool_name")
            r["inputSchema"] = r["input_schema"]
            results.append(r)
        return results

    async def create_build(self, build: Dict[str, Any]) -> None:
        import datetime as _dt
        now = datetime.now(timezone.utc).isoformat()
        bid = build.get("build_id") or build.get("id") or f"mcpb-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO pg_mcp_builds (id, user_id, name, method, source,
                 status, stage, logs_json, mcp_id, tools_json, error, origin,
                 parent_run_id, parent_workflow_id, parent_task_id,
                 required_operations_json, intake_json)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
               ON CONFLICT (id) DO NOTHING""",
            bid, build.get("user_id", ""), build.get("name", ""),
            build.get("method", "spec"), build.get("source"),
            build.get("status", "pending"), build.get("stage", ""),
            build.get("logs_json", "[]"), build.get("mcp_id"),
            build.get("tools_json", "[]"), build.get("error"),
            build.get("origin", "user"), build.get("parent_run_id"),
            build.get("parent_workflow_id"), build.get("parent_task_id"),
            json.dumps(build.get("required_operations") or []),
            json.dumps(build.get("intake") or {}))
        await self.db.execute(
            "UPDATE pg_mcp_builds SET build_id=$2 WHERE id=$1", bid, bid)

    async def update_build(self, build_id: str, updates: Dict[str, Any]) -> None:
        updates = dict(updates)
        for source, target in (("logs", "logs_json"), ("tools", "tools_json"),
                               ("intake", "intake_json"),
                               ("required_operations",
                                "required_operations_json")):
            if source in updates:
                updates[target] = updates[source]
        sets, args, idx = [], [], 1
        for key in ("status", "mcp_id", "error", "stage", "tools_json",
                    "logs_json", "intake_json", "required_operations_json"):
            if key in updates:
                sets.append(f"{key}=${idx}")
                args.append(updates[key] if not isinstance(updates[key], (dict, list))
                            else json.dumps(updates[key]))
                idx += 1
        if not sets:
            return
        args.append(build_id)
        await self.db.execute(
            f"UPDATE pg_mcp_builds SET {', '.join(sets)} WHERE id=${idx}",
            *args)

    async def get_build(self, build_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_mcp_builds WHERE id=$1 OR build_id=$1",
            build_id)
        return self._build_shape(row) if row else None

    @staticmethod
    def _build_shape(row):
        out = dict(row)
        for k in ("logs_json", "tools_json"):
            out[k] = _parse_json(out.get(k))
        out["logs"] = out.get("logs_json") or []
        out["tools"] = out.get("tools_json") or []
        out["required_operations"] = _parse_json(out.get("required_operations_json")) or []
        out["intake"] = _parse_json(out.get("intake_json")) or {}
        out["build_id"] = out.get("build_id") or out.get("id")
        return out

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        contract = _parse_json(row.get("contract_metadata")) or {}
        out = {**contract, **dict(row)}
        out.pop("contract_metadata", None)
        out["is_enabled"] = out["enabled"]
        out["auth"] = {**(contract.get("auth") or {}), "type": str(out.get("auth_type") or "NONE").upper()}
        if out.get("credential_ref"):
            out["auth"]["credential_name"] = out["credential_ref"]
        out["legacy_metadata"] = _parse_json(out.get("legacy_metadata"))
        out["metadata"] = out.get("legacy_metadata") or {}
        # Cross-repo contract parity: SQLite/Firestore rows (and the
        # shared MCPManifest contract) key identity as mcp_id + endpoint.
        # Emit both spellings so consumers never depend on the storage.
        out.setdefault("mcp_id", out.get("id"))
        out.setdefault("endpoint", out.get("base_url"))
        if isinstance(out.get("test_coverage"), str):
            out["test_coverage"] = _parse_json(out["test_coverage"]) or {}
        if out.get("created_at"):
            out["created_at"] = out["created_at"].isoformat()
        if out.get("updated_at"):
            out["updated_at"] = out["updated_at"].isoformat()
        return out


# ═══════════════════════════════════════════════════════════════════════
# Commands + outbox + approvals
# ═══════════════════════════════════════════════════════════════════════

class PostgresCommandRepository(_Base):
    """Durable command receipt + outbox with lease-based dispatch —
    the §21.4 cross-process path (workers claim outbox batches)."""

    async def record_command(self, command: Dict[str, Any]) -> str:
        cid = command.get("id") or f"cmd-{uuid.uuid4().hex[:14]}"
        await self.db.execute(
            """INSERT INTO pg_commands (id, user_id, run_id, command_type,
                 state, payload, idempotency_key, deadline_at)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
               ON CONFLICT (idempotency_key) DO NOTHING""",
            cid, command.get("user_id", ""), command.get("run_id"),
            command.get("command_type", ""),
            command.get("state", "RECEIVED"),
            _json(command.get("payload", {})),
            command.get("idempotency_key"), command.get("deadline_at"))
        return cid

    async def get_command(self, command_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_commands WHERE id=$1", command_id)
        return self._shape(row) if row else None

    async def get_command_by_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_commands WHERE run_id=$1 "
            "ORDER BY created_at DESC LIMIT 1", run_id)
        return self._shape(row) if row else None

    async def transition(self, command_id: str, target: str, *,
                         actor: str = "system", reason: str = "") -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_commands SET state=$2, payload = payload || "
            "$3::jsonb, updated_at=now() WHERE id=$1 RETURNING id",
            command_id, target, _json({"_last_transition": {
                "actor": actor, "reason": reason, "at": _now().isoformat()}}))
        return row is not None

    async def append_outbox(self, event_id: str, topic: str,
                            payload: Dict[str, Any], *,
                           available_at: Optional[datetime] = None) -> None:
        await self.db.execute(
            """INSERT INTO pg_outbox_events (event_id, topic, payload,
                 available_at) VALUES ($1,$2,$3::jsonb,
                 COALESCE($4, now()))
               ON CONFLICT (event_id) DO NOTHING""",
            event_id, topic, _json(payload), available_at)

    async def claim_outbox_batch(self, owner: str, batch: int = 20,
                                 lease_seconds: int = 60) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """WITH claimed AS (
                 SELECT id FROM pg_outbox_events
                 WHERE state='PENDING' AND available_at <= now()
                 ORDER BY id LIMIT $2
                 FOR UPDATE SKIP LOCKED)
               UPDATE pg_outbox_events o
               SET state='DISPATCHING', lease_owner=$1,
                   lease_expires_at=now() + make_interval(secs => $3),
                   attempts = o.attempts + 1
               FROM claimed c WHERE o.id = c.id
               RETURNING o.id, o.event_id, o.topic, o.payload, o.attempts""",
            owner, batch, lease_seconds)
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = _parse_json(item["payload"])
            out.append(item)
        return out

    async def mark_outbox_published(self, event_id: str) -> None:
        await self.db.execute(
            "UPDATE pg_outbox_events SET state='PUBLISHED', published_at=now() "
            "WHERE event_id=$1", event_id)

    async def fail_outbox_event(self, event_id: str, error: str) -> None:
        await self.db.execute(
            "UPDATE pg_outbox_events SET state='FAILED', error=$2 "
            "WHERE event_id=$1", event_id, error[:500])

    async def reclaim_stale_outbox_leases(self) -> int:
        rows = await self.db.fetch_all(
            "UPDATE pg_outbox_events SET state='PENDING', lease_owner=NULL "
            "WHERE state='DISPATCHING' AND lease_expires_at < now() "
            "RETURNING event_id")
        return len(rows)


class PostgresApprovalRepository(_Base):
    async def create_approval(self, approval: Dict[str, Any]) -> str:
        aid = approval.get("id") or f"apr-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO pg_approvals (id, user_id, run_id, task_id, kind,
                 payload) VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
            aid, approval.get("user_id", ""), approval.get("run_id"),
            approval.get("task_id"), approval.get("kind", "tool_approval"),
            _json(approval.get("payload", {})))
        return aid

    async def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_approvals WHERE id=$1", approval_id)
        return self._shape(row) if row else None

    async def list_pending(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_approvals WHERE user_id=$1 AND status='PENDING' "
            "ORDER BY created_at", user_id)
        return [self._shape(r) for r in rows]

    async def resolve(self, approval_id: str, new_status: str,
                      decided_by: str) -> bool:
        row = await self.db.fetch_one(
            """UPDATE pg_approvals SET status=$2, decision_by=$3,
               resolved_at=now() WHERE id=$1 AND status='PENDING'
               RETURNING id""", approval_id, new_status, decided_by)
        return row is not None

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["payload"] = _parse_json(out.get("payload"))
        return out


# ═══════════════════════════════════════════════════════════════════════
# Effects (external actions) — digest-anchored idempotency
# ═══════════════════════════════════════════════════════════════════════

class PostgresEffectRepository(_Base):
    async def prepare_effect(self, effect: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Digest-anchored prepare: one ACTIVE effect per
        (owner, operation, digest). Concurrent prepares serialize on the
        unique partial index; the loser returns the winner's identity."""
        effect_id = effect.get("effect_id") or f"fx_{uuid.uuid4().hex[:24]}"
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT * FROM pg_effects WHERE owner_user_id=$1 "
                    "AND operation_name=$2 AND request_digest=$3 "
                    "AND state <> 'CANCELLED' FOR UPDATE",
                    effect.get("owner_user_id", ""),
                    effect.get("operation_name", ""),
                    effect.get("request_digest", ""))
                if existing is not None:
                    return self._shape(dict(existing)), False
                await conn.execute(
                    """INSERT INTO pg_effects (effect_id, operation_name,
                         owner_user_id, run_id, task_id, target,
                         canonical_arguments, request_digest,
                         external_account_id, credential_reference,
                         approval_reference)
                       VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11)""",
                    effect_id, effect.get("operation_name", ""),
                    effect.get("owner_user_id", ""), effect.get("run_id"),
                    effect.get("task_id"), effect.get("target", ""),
                    _json(effect.get("canonical_arguments", {})),
                    effect.get("request_digest", ""),
                    effect.get("external_account_id"),
                    effect.get("credential_reference"),
                    effect.get("approval_reference"))
        return await self.get_effect(effect_id), True

    async def get_effect(self, effect_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_effects WHERE effect_id=$1", effect_id)
        return self._shape(row) if row else None

    async def transition(self, effect_id: str, target: str, *,
                         expected_version: Optional[int] = None,
                         actor: str = "system", reason: str = "") -> Dict[str, Any]:
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                if expected_version is not None:
                    row = await conn.fetchrow(
                        "UPDATE pg_effects SET state=$2, "
                        "state_version=state_version+1, updated_at=now() "
                        "WHERE effect_id=$1 AND state_version=$3 "
                        "RETURNING *", effect_id, target, expected_version)
                else:
                    row = await conn.fetchrow(
                        "UPDATE pg_effects SET state=$2, "
                        "state_version=state_version+1, updated_at=now() "
                        "WHERE effect_id=$1 RETURNING *", effect_id, target)
                if row is None:
                    raise ValueError(
                        f"effect {effect_id}: stale version or missing "
                        f"(expected {expected_version})")
                return self._shape(dict(row))

    async def record_receipt(self, effect_id: str, attempt_id: str,
                             evidence: Dict[str, Any],
                             response_digest: str = "",
                             provider: str = "") -> None:
        await self.db.execute(
            """INSERT INTO pg_effect_receipts (effect_id, attempt_id,
                 provider, evidence, response_digest)
               VALUES ($1,$2,$3,$4::jsonb,$5)""",
            effect_id, attempt_id, provider, _json(evidence), response_digest)

    async def receipts_for(self, effect_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_effect_receipts WHERE effect_id=$1 "
            "ORDER BY id", effect_id)
        return [self._shape(r) for r in rows]

    async def unresolved_effects(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_effects WHERE state IN ('DISPATCHING', "
            "'OUTCOME_UNKNOWN','PROVIDER_PENDING') ORDER BY updated_at "
            "LIMIT $1", limit)
        return [self._shape(r) for r in rows]

    async def effects_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_effects WHERE run_id=$1 ORDER BY created_at",
            run_id)
        return [self._shape(r) for r in rows]

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["canonical_arguments"] = _parse_json(out.get("canonical_arguments"))
        if out.get("created_at"):
            out["created_at"] = int(out["created_at"].timestamp())
        if out.get("updated_at"):
            out["updated_at"] = int(out["updated_at"].timestamp())
        return out


# ═══════════════════════════════════════════════════════════════════════
# Browser session ownership (durable, §21.13)
# ═══════════════════════════════════════════════════════════════════════

class _LegacyPostgresBrowserSessionRepository(_Base):
    async def register_worker_session(self, *, session_id, worker_id, endpoint,
                                      owner_user_id, run_id, task_id, controller_epoch):
        await self.db.execute(
            """INSERT INTO pg_browser_sessions
               (session_id,user_id,run_id,task_id,owner_worker_id,runtime_session_id,
                controller_epoch,lease_expires_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,now()+interval '1 hour')""",
            session_id, owner_user_id, run_id, task_id, worker_id, endpoint, controller_epoch)

    async def list_for_owner(self, owner_user_id):
        return await self.db.fetch_all(
            """SELECT session_id,run_id,task_id,owner_worker_id FROM pg_browser_sessions
               WHERE user_id=$1 AND owner_worker_id IS NOT NULL AND state <> 'CLOSED'
               AND lease_expires_at > now() ORDER BY created_at DESC""", owner_user_id)

    async def close_session(self, session_id):
        await self.transition(session_id, "CLOSED")

    async def create_session(self, session: Dict[str, Any]) -> str:
        sid = session.get("session_id") or f"bs-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO pg_browser_sessions (session_id, user_id, run_id,
                 task_id, controller_worker_id, controller_epoch,
                 lease_expires_at, allowed_origins)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
            sid, session.get("user_id", ""), session.get("run_id", ""),
            session.get("task_id", ""), session.get("controller_worker_id"),
            session.get("controller_epoch"), session.get("lease_expires_at"),
            _json(session.get("allowed_origins", [])))
        return sid

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_browser_sessions WHERE session_id=$1",
            session_id)
        return self._shape(row) if row else None

    async def get_session_for_run(self, run_id: str,
                                  task_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_browser_sessions WHERE run_id=$1 AND task_id=$2 "
            "ORDER BY created_at DESC LIMIT 1", run_id, task_id)
        return self._shape(row) if row else None

    async def acquire_ownership(self, session_id: str, worker_id: str,
                                epoch: str, lease_seconds: int) -> bool:
        row = await self.db.fetch_one(
            """UPDATE pg_browser_sessions
               SET controller_worker_id=$2, controller_epoch=$3,
                   lease_expires_at=now() + make_interval(secs => $4),
                   last_activity_at=now()
               WHERE session_id=$1 AND (
                     controller_worker_id IS NULL OR
                     lease_expires_at < now() OR
                     controller_worker_id=$2)
               RETURNING session_id""",
            session_id, worker_id, epoch, lease_seconds)
        return row is not None

    async def renew_ownership(self, session_id: str, worker_id: str,
                              lease_seconds: int) -> bool:
        row = await self.db.fetch_one(
            """UPDATE pg_browser_sessions
               SET lease_expires_at=now() + make_interval(secs => $3),
                   last_activity_at=now()
               WHERE session_id=$1 AND controller_worker_id=$2
               RETURNING session_id""", session_id, worker_id, lease_seconds)
        return row is not None

    async def set_controller(self, session_id: str, worker_id: str,
                             epoch: str) -> None:
        """Takeover/return-control: force-set the controller (the caller
        has passed the server-side authorization)."""
        await self.db.execute(
            "UPDATE pg_browser_sessions SET controller_worker_id=$2, "
            "controller_epoch=$3, last_activity_at=now() WHERE session_id=$1",
            session_id, worker_id, epoch)

    async def transition(self, session_id: str, state: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_browser_sessions SET state=$2, last_activity_at=now() "
            "WHERE session_id=$1 RETURNING session_id", session_id, state)
        return row is not None

    async def set_challenge(self, session_id: str,
                            challenge: Optional[Dict[str, Any]]) -> None:
        await self.db.execute(
            "UPDATE pg_browser_sessions SET challenge=$2::jsonb "
            "WHERE session_id=$1", session_id,
            _json(challenge) if challenge else None)

    async def record_checkpoint(self, session_id: str, kind: str,
                                payload: Dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO pg_browser_checkpoints (session_id, kind, payload)
               VALUES ($1,$2,$3::jsonb)""", session_id, kind, _json(payload))

    async def latest_checkpoint(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_browser_checkpoints WHERE session_id=$1 "
            "ORDER BY id DESC LIMIT 1", session_id)
        if not row:
            return None
        out = dict(row)
        out["payload"] = _parse_json(out.get("payload"))
        return out

    async def record_activity(self, session_id: str) -> None:
        await self.db.execute(
            "UPDATE pg_browser_sessions SET last_activity_at=now() "
            "WHERE session_id=$1", session_id)

    async def expired_sessions(self, lease_seconds: int) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM pg_browser_sessions WHERE state='ACTIVE' AND "
            "last_activity_at < now() - make_interval(secs => $1)",
            lease_seconds)

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["allowed_origins"] = _parse_json(out.get("allowed_origins"))
        out["challenge"] = _parse_json(out.get("challenge"))
        return out


# ═══════════════════════════════════════════════════════════════════════
# Recovery episodes
# ═══════════════════════════════════════════════════════════════════════

class PostgresBrowserSessionRepository(PostgresBrowserContract, _LegacyPostgresBrowserSessionRepository):
    """Typed WebAgent lifecycle and backward-compatible viewer routing."""


class PostgresRecoveryEpisodeRepository(_Base):
    TERMINAL = ("VERIFIED_RECOVERY", "RECOVERY_FAILED", "CANCELLED",
                "SUPERSEDED", "ESCALATED")

    async def create_episode(self, episode: Dict[str, Any]) -> Dict[str, Any]:
        eid = episode.get("episode_id") or f"ep-{uuid.uuid4().hex[:16]}"
        row = await self.db.fetch_one(
            """INSERT INTO pg_recovery_episodes (episode_id, tenant_id,
                 actor_id, run_id, task_id, failure_code, failure_category,
                 goal_digest, acceptance_criteria, payload)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
               ON CONFLICT (episode_id) DO NOTHING RETURNING episode_id""",
            eid, episode.get("tenant_id", ""), episode.get("actor_id", "system"),
            episode.get("run_id", ""), episode.get("task_id", ""),
            episode.get("failure_code", ""), episode.get("failure_category", ""),
            episode.get("goal_digest", ""),
            _json(episode.get("acceptance_criteria", [])),
            _json(episode.get("payload", {})))
        if row is None:
            return await self.get_episode(eid)
        return await self.get_episode(eid)

    async def get_episode(self, episode_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_recovery_episodes WHERE episode_id=$1",
            episode_id)
        return self._shape(row) if row else None

    async def get_episode_for_task(self, run_id: str,
                                   task_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """SELECT * FROM pg_recovery_episodes WHERE run_id=$1
               AND task_id=$2
               AND state NOT IN ('VERIFIED_RECOVERY','RECOVERY_FAILED',
                 'CANCELLED','SUPERSEDED','ESCALATED')
               ORDER BY created_at DESC LIMIT 1""", run_id, task_id)
        return self._shape(row) if row else None

    async def claim_episode(self, episode_id: str, worker_id: str,
                            lease_seconds: int) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """UPDATE pg_recovery_episodes
               SET owner_worker_id=$2,
                   owner_generation=owner_generation+1,
                   lease_expires_at=now() + make_interval(secs => $3),
                   updated_at=now()
               WHERE episode_id=$1 AND state NOT IN
                 ('VERIFIED_RECOVERY','RECOVERY_FAILED','CANCELLED',
                  'SUPERSEDED','ESCALATED')
                 AND (owner_worker_id IS NULL OR owner_worker_id=$2
                      OR lease_expires_at < now())
               RETURNING *""", episode_id, worker_id, lease_seconds)
        return self._shape(dict(row)) if row else None

    async def transition(self, episode_id: str, target: str, *,
                         expected_version: Optional[int] = None,
                         actor: str = "system") -> Dict[str, Any]:
        if expected_version is not None:
            row = await self.db.fetch_one(
                "UPDATE pg_recovery_episodes SET state=$2, "
                "state_version=state_version+1, updated_at=now() "
                "WHERE episode_id=$1 AND state_version=$3 RETURNING *",
                episode_id, target, expected_version)
        else:
            row = await self.db.fetch_one(
                "UPDATE pg_recovery_episodes SET state=$2, "
                "state_version=state_version+1, updated_at=now() "
                "WHERE episode_id=$1 RETURNING *", episode_id, target)
        if row is None:
            raise ValueError(f"episode {episode_id}: transition refused")
        return self._shape(row)

    async def increment_counters(self, episode_id: str, *, model_calls: int = 0,
                                 tool_calls: int = 0,
                                 verification_calls: int = 0,
                                 attempt_count: int = 0) -> None:
        await self.db.execute(
            """UPDATE pg_recovery_episodes SET
                 model_calls = model_calls + $2,
                 tool_calls = tool_calls + $3,
                 verification_calls = verification_calls + $4,
                 attempt_count = attempt_count + $5,
                 updated_at = now()
               WHERE episode_id=$1""",
            episode_id, model_calls, tool_calls, verification_calls,
            attempt_count)

    async def reviewed_failures(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_recovery_episodes WHERE state='RECOVERY_FAILED' "
            "ORDER BY updated_at DESC LIMIT $1", limit)
        return [self._shape(r) for r in rows]

    async def abandoned_episodes(self, older_than_seconds: int) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT * FROM pg_recovery_episodes WHERE state NOT IN
               ('VERIFIED_RECOVERY','RECOVERY_FAILED','CANCELLED',
                'SUPERSEDED','ESCALATED')
               AND updated_at < now() - make_interval(secs => $1)""",
            older_than_seconds)
        return [self._shape(r) for r in rows]

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["acceptance_criteria"] = _parse_json(out.get("acceptance_criteria"))
        out["payload"] = _parse_json(out.get("payload"))
        return out


# ═══════════════════════════════════════════════════════════════════════
# Evaluations + memory (durable dependencies §21.19) + deliverables
# ═══════════════════════════════════════════════════════════════════════

class PostgresEvaluationRepository(_Base):
    async def save_evaluation(self, evaluation: Dict[str, Any]) -> str:
        eid = evaluation.get("id") or f"ev-{uuid.uuid4().hex[:14]}"
        await self.db.execute(
            """INSERT INTO pg_evaluations (id, user_id, run_id, task_id,
                 identity_digest, outcome, metrics, evidence)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)""",
            eid, evaluation.get("user_id", ""), evaluation.get("run_id", ""),
            evaluation.get("task_id"), evaluation.get("identity_digest", ""),
            evaluation.get("outcome", ""), _json(evaluation.get("metrics", {})),
            _json(evaluation.get("evidence", {})))
        return eid

    async def get_for_identity(self, identity_digest: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_evaluations WHERE identity_digest=$1 "
            "ORDER BY created_at DESC LIMIT 1", identity_digest)
        return self._shape(row) if row else None

    async def get_evaluation(self, evaluation_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_evaluations WHERE id=$1", evaluation_id)
        return self._shape(row) if row else None

    async def evaluations_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_evaluations WHERE run_id=$1 ORDER BY created_at",
            run_id)
        return [self._shape(r) for r in rows]

    async def evaluation_summary(self, run_id: str) -> Dict[str, Any]:
        rows = await self.db.fetch_all(
            "SELECT outcome, count(*) AS n FROM pg_evaluations "
            "WHERE run_id=$1 GROUP BY outcome", run_id)
        return {r["outcome"]: int(r["n"]) for r in rows}

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["metrics"] = _parse_json(out.get("metrics"))
        out["evidence"] = _parse_json(out.get("evidence"))
        return out


class PostgresMemoryRepository(_Base):
    """Memory with DURABLE tenant-scoped dependencies (§21.19) — the
    dependency graph lives in pg_memory_dependencies, not in a process
    map, so correction/deletion propagation survives restarts."""

    async def store_memory(self, user_id: str, content: str, memory_type: Optional[str] = None,
                           metadata: Optional[Dict[str, Any]] = None,
                           embedding: Optional[List[float]] = None,
                           memory_id: str = "", *, kind: Optional[str] = None) -> str:
        if kind is not None and memory_type is not None and memory_type != kind:
            raise ValueError("Conflicting memory types")
        memory_type = memory_type or kind
        if not memory_type:
            raise ValueError("Memory type is required")
        meta = dict(metadata or {})
        meta["memory_type"] = memory_type
        mid = memory_id or meta.get("memory_id") or f"mem-{uuid.uuid4().hex[:16]}"
        # Idempotent on the source event identity (§16.5).
        source_event_id = meta.get("source_event_id")
        async with self.db.pool.acquire() as conn, conn.transaction():
            if source_event_id:
                # Serialize duplicate deliveries without changing historical IDs.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,1702))",
                                   _json([user_id, source_event_id]))
                existing = await conn.fetchval(
                    "SELECT id FROM pg_memories WHERE user_id=$1 "
                    "AND COALESCE(source_event_id, metadata->>'source_event_id')=$2 "
                    "ORDER BY created_at,id LIMIT 1", user_id, source_event_id)
                if existing:
                    return existing
            await conn.execute(
                """INSERT INTO pg_memories (id, user_id, content, memory_class,
                 embedding, metadata, source, scope, scope_ref, subject_key,
                 verification_status, source_event_id, observed_at, expires_at)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8,$9,$10,$11,$12,
                 $13,$14)""",
                mid, user_id, content, meta.get("memory_class", memory_type),
                _json(embedding) if embedding else None, _json(meta),
                meta.get("source"), meta.get("scope", "personal"),
                meta.get("scope_ref"), meta.get("subject_key"),
                meta.get("verification_status", "inferred"),
                source_event_id, meta.get("observed_at"), meta.get("expires_at"))
        return mid

    async def get_memory(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_memories WHERE user_id=$1 AND id=$2 AND deletion_state='active'",
            user_id, memory_id)
        return self._shape(row) if row else None

    async def search_memory(self, user_id: str, query_embedding: List[float],
                            limit: int = 8,
                            extra_owners: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Cosine similarity in Python over the candidate set (small local
        scale; pgvector is an optional later optimization)."""
        owners = [user_id] + list(extra_owners or [])
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_memories WHERE user_id = ANY($1) "
            "AND deletion_state='active' AND "
            "(expires_at IS NULL OR expires_at > "
            "EXTRACT(EPOCH FROM now()))", owners)
        import math
        scored = []
        for r in rows:
            emb = _parse_json(r.get("embedding"))
            if not emb or not query_embedding:
                continue
            dot = sum(a * b for a, b in zip(emb, query_embedding))
            na = math.sqrt(sum(a * a for a in emb)) or 1e-9
            nb = math.sqrt(sum(b * b for b in query_embedding)) or 1e-9
            scored.append((dot / (na * nb), r))
        scored.sort(key=lambda t: -t[0])
        return [self._shape(r) for _, r in scored[:limit]]

    async def list_memories(self, user_id: str,
                            memory_type: str = None,
                            limit: int = 50,
                            offset: int = 0) -> List[Dict[str, Any]]:
        """SQLite contract parity: memory_type filter, limit/offset paging
        (Phase 27: the PG implementation was missing these kwargs,
        causing a 500 on the memory listing endpoint)."""
        if memory_type:
            rows = await self.db.fetch_all(
                "SELECT * FROM pg_memories WHERE user_id=$1 AND deletion_state='active' "
                "AND COALESCE(metadata->>'memory_type', memory_class)=$2 "
                "ORDER BY created_at DESC, id LIMIT $3 OFFSET $4",
                user_id, memory_type, limit, offset)
        else:
            rows = await self.db.fetch_all(
                "SELECT * FROM pg_memories WHERE user_id=$1 AND deletion_state='active' "
                "ORDER BY created_at DESC, id LIMIT $2 OFFSET $3",
                user_id, limit, offset)
        return [self._shape(r) for r in rows]

    async def update_memory(self, user_id: str, memory_id: str,
                            updates: Dict[str, Any]) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_memories SET content=$3, metadata="
            "metadata || $4::jsonb, updated_at=now() "
            "WHERE user_id=$1 AND id=$2 RETURNING id",
            user_id, memory_id, updates.get("content", ""),
            _json(updates.get("metadata", {})))
        return row is not None

    async def supersede_older_for_subject(self, user_id: str, subject_key: str,
                                          keep_id: str) -> int:
        rows = await self.db.fetch_all(
            "UPDATE pg_memories SET superseded_by=$3, updated_at=now() "
            "WHERE user_id=$1 AND subject_key=$2 AND id <> $3 "
            "AND superseded_by IS NULL RETURNING id", user_id, subject_key,
            keep_id)
        return len(rows)

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        return (await self.forget_with_cleanup(user_id, memory_id))["deleted"]

    async def forget_with_cleanup(self, user_id: str, memory_id: str) -> dict:
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE pg_memories SET deletion_state='deleted', updated_at=now() "
                "WHERE user_id=$1 AND id=$2 AND deletion_state='active' RETURNING metadata",
                user_id, memory_id)
            if not row:
                return {"deleted": False, "propagated": []}
            deps = await self._cleanup_dependencies(conn, memory_id, row["metadata"])
            job_id = await conn.fetchval(
                "INSERT INTO pg_memory_cleanup_jobs (memory_id,user_id,dependents) "
                "VALUES ($1,$2,$3::jsonb) RETURNING id", memory_id, user_id, _json(deps))
        return {"deleted": True, "propagated": [d["id"] for d in deps],
                "cleanup_job_id": job_id}

    @staticmethod
    async def _cleanup_dependencies(conn, memory_id, metadata):
        edges = await conn.fetch("SELECT derived_kind AS kind, derived_id AS id "
                                 "FROM pg_memory_dependencies WHERE memory_id=$1", memory_id)
        deps = [dict(edge) for edge in edges]
        meta = _parse_json(metadata) or {}
        deps.extend({"kind": "metadata_ref", "id": str(ref)} for ref in meta.get("derived_refs") or [])
        return deps

    # ── DURABLE dependencies (§21.19) ─────────────────────────────────
    async def register_dependency(self, memory_id: str, *, derived_kind: str,
                                  derived_id: str) -> str:
        async with self.db.pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT id FROM pg_memories WHERE id=$1 "
                                       "AND deletion_state='active' FOR SHARE", memory_id):
                raise ValueError("Cannot add a dependency to an absent or deleted memory")
            await conn.execute(
                """INSERT INTO pg_memory_dependencies (memory_id,
                     derived_kind, derived_id) VALUES ($1,$2,$3)
                   ON CONFLICT (memory_id, derived_kind, derived_id)
                   DO NOTHING""", memory_id, derived_kind, derived_id)
        return memory_id

    async def dependents_of(self, memory_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT id AS edge_id, memory_id, derived_kind, derived_id, created_at "
            "FROM pg_memory_dependencies WHERE memory_id=$1", memory_id)

    async def create_cleanup_job(self, memory_id: str, *, user_id=None, dependents=None) -> int:
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT user_id,metadata FROM pg_memories WHERE id=$1 "
                                      "AND ($2::text IS NULL OR user_id=$2) FOR SHARE", memory_id, user_id)
            if not row:
                raise ValueError("Memory owner could not be verified")
            if dependents is None:
                dependents = await self._cleanup_dependencies(conn, memory_id, row["metadata"])
            return await conn.fetchval("INSERT INTO pg_memory_cleanup_jobs (memory_id,user_id,dependents) "
                                       "VALUES ($1,$2,$3::jsonb) RETURNING id",
                                       memory_id, row["user_id"], _json(dependents))

    async def claim_cleanup_job(self, owner: str,
                                lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """WITH c AS (
                 SELECT id FROM pg_memory_cleanup_jobs
                 WHERE state='pending' OR (state='failed' AND attempts<5) OR (state='running'
                       AND lease_expires_at < now())
                 ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
               UPDATE pg_memory_cleanup_jobs j
               SET state='running', lease_owner=$1,
                   lease_expires_at=now() + make_interval(secs => $2),
                   attempts=j.attempts+1, updated_at=now()
               FROM c WHERE j.id=c.id RETURNING j.*""", owner, lease_seconds)
        if not row:
            return None
        job = {**row, "job_id": row["id"], "dependents": _parse_json(row["dependents"])}
        if job["dependents"] is None or job["user_id"] is None:
            # Jobs written before 0022 retain their source and dependency edges.
            async with self.db.pool.acquire() as conn:
                source = await conn.fetchrow("SELECT user_id,metadata FROM pg_memories WHERE id=$1",
                                             job["memory_id"])
                if source:
                    job["user_id"] = source["user_id"]
                    job["dependents"] = await self._cleanup_dependencies(conn, job["memory_id"], source["metadata"])
            if not source:
                await self.record_cleanup_progress(job["id"], worker_id=owner, state="failed",
                                                   error="Legacy memory owner missing")
                raise ValueError("Legacy memory owner missing")
        return job

    async def record_cleanup_progress(self, job_id: int, state: str, error=None, *, worker_id: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_memory_cleanup_jobs SET state=$2, last_error=$3, "
            "lease_owner=NULL, lease_expires_at=NULL, updated_at=now() "
            "WHERE id=$1 AND state='running' AND lease_owner=$4 AND lease_expires_at>now() RETURNING id",
            job_id, state, (error or "")[:500], worker_id)
        return row is not None

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["metadata"] = _parse_json(out.get("metadata"))
        out["embedding"] = _parse_json(out.get("embedding"))
        out["memory_type"] = (out["metadata"] or {}).get("memory_type") or out["memory_class"]
        if out.get("created_at"):
            out["created_at"] = int(out["created_at"].timestamp())
        return out


class PostgresDeliverableRepository(_Base):
    """§17.7 deliverable contract on PostgreSQL — SAME signatures and
    staged-lifecycle semantics as the canonical SQLite repository
    (backend/repositories/deliverable_repository.py). Phase 27 found the
    legacy dict contract here refusing the production publication path;
    migration 0023 carries the revision-key and verification columns."""

    @staticmethod
    def _row_to_deliverable(row: Dict[str, Any]):
        from backend.models.deliverable import Deliverable
        d = Deliverable(
            deliverable_id=row["id"],
            deliverable_key=row.get("deliverable_key") or "",
            revision=int(row.get("revision") or 1),
            parent_revision=row.get("parent_revision"),
            tenant_id=row.get("tenant_id") or "",
            owner_user_id=row.get("owner_user_id") or row.get("user_id") or "",
            run_id=row.get("run_id") or "",
            task_id=row.get("task_id") or "",
            deliverable_type=row.get("deliverable_type") or "artifact",
            mime_type=row.get("mime_type") or "text/markdown",
            file_manifest=_parse_json(row.get("file_manifest")) or [],
            content_checksum=row.get("content_checksum") or "",
            lineage=_parse_json(row.get("lineage")) or {},
            verification=_parse_json(row.get("verification_json"))
            or _parse_json(row.get("verification")) or {},
            lifecycle_state=row.get("lifecycle_state") or "generated",
            access_policy=row.get("access_policy") or "owner",
            created_at=row["created_at"].timestamp() if row.get("created_at") else 0,
            updated_at=row["updated_at"].timestamp() if row.get("updated_at") else 0)
        return d

    async def create_candidate(self, d, *,
                               deliverable_key: Optional[str] = None):
        """§17.7 stage 1: allocate the durable GENERATION identity. A
        candidate is unpublished — a crash before publish leaves an
        unpublished candidate, never a completed artifact."""
        from backend.models.deliverable import Deliverable
        if not isinstance(d, Deliverable):
            raise TypeError("create_candidate requires a Deliverable model")
        now = datetime.now(timezone.utc)
        key = deliverable_key or d.deliverable_key or f"dlvk_{uuid.uuid4().hex[:16]}"
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO pg_deliverable_keys (deliverable_key,
                         current_revision, updated_at) VALUES ($1, 0, now())
                       ON CONFLICT (deliverable_key) DO NOTHING""", key)
                row = await conn.fetchval(
                    "SELECT current_revision FROM pg_deliverable_keys "
                    "WHERE deliverable_key=$1 FOR UPDATE", key)
                revision = int(row or 0) + 1
                parent = revision - 1 if revision > 1 else None
                await conn.execute(
                    "UPDATE pg_deliverable_keys SET current_revision=$2, "
                    "updated_at=now() WHERE deliverable_key=$1", key, revision)
                did = f"dlv-{uuid.uuid4().hex[:18]}"
                await conn.execute(
                    """INSERT INTO pg_deliverables (id, deliverable_key,
                         revision, parent_revision, tenant_id, owner_user_id,
                         user_id, run_id, task_id, deliverable_type, mime_type,
                         file_manifest, content_checksum, lineage,
                         verification_json, lifecycle_state, access_policy,
                         created_at, updated_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,$9,$10,
                               $11::jsonb,'',$12::jsonb,$13::jsonb,
                               'generated',$14,$15,$15)""",
                    did, key, revision, parent, d.tenant_id, d.owner_user_id,
                    d.run_id, d.task_id, d.deliverable_type, d.mime_type,
                    _json(d.file_manifest), _json(d.lineage),
                    _json(d.verification), d.access_policy, now)
        d.deliverable_id = did
        d.deliverable_key = key
        d.revision = revision
        d.parent_revision = parent
        d.created_at = d.updated_at = now.timestamp()
        return d

    async def get(self, deliverable_id: str):
        row = await self.db.fetch_one(
            "SELECT * FROM pg_deliverables WHERE id=$1", deliverable_id)
        return self._row_to_deliverable(row) if row else None

    async def get_for_owner(self, deliverable_id: str, owner_user_id: str):
        """Ownership-first: a wrong-user lookup looks identical to
        missing (§12.28)."""
        row = await self.db.fetch_one(
            "SELECT * FROM pg_deliverables WHERE id=$1 AND owner_user_id=$2",
            deliverable_id, owner_user_id)
        return self._row_to_deliverable(row) if row else None

    async def list_for_owner(self, owner_user_id: str,
                             limit: int = 100, offset: int = 0):
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_deliverables WHERE owner_user_id=$1 AND "
            "lifecycle_state != 'generated' ORDER BY updated_at DESC "
            "LIMIT $2 OFFSET $3", owner_user_id, limit, offset)
        return [self._row_to_deliverable(r) for r in rows]

    async def transition(self, deliverable_id: str, target: str, *,
                         verification: Optional[Dict[str, Any]] = None,
                         file_manifest: Optional[List[Dict[str, Any]]] = None,
                         content_checksum: Optional[str] = None,
                         actor: str = "system"):
        """The staged lifecycle (§17.7/§17.10): generated → stored →
        verification_completed → verification_passed/failed →
        review_completed → delivered. `delivered` requires registered
        bytes (the published bytes ARE the identity)."""
        from backend.models.deliverable import LIFECYCLE_STATES
        if target not in LIFECYCLE_STATES:
            raise ValueError(f"unknown lifecycle state '{target}'")
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT file_manifest FROM pg_deliverables "
                    "WHERE id=$1 FOR UPDATE", deliverable_id)
                if not row:
                    raise ValueError("deliverable not found")
                if target == "delivered" and content_checksum is None and \
                        not (_parse_json(row["file_manifest"]) or []):
                    raise ValueError(
                        "cannot deliver without registered bytes (§17.7)")
                await conn.execute(
                    """UPDATE pg_deliverables SET lifecycle_state=$2,
                       verification_json=COALESCE($3::jsonb, verification_json),
                       file_manifest=COALESCE($4::jsonb, file_manifest),
                       content_checksum=COALESCE($5, content_checksum),
                       updated_at=now() WHERE id=$1""",
                    deliverable_id, target,
                    _json(verification) if verification is not None else None,
                    _json(file_manifest) if file_manifest is not None else None,
                    content_checksum)
        return await self.get(deliverable_id)

    async def current_revision_for(self, deliverable_key: str) -> int:
        row = await self.db.fetch_one(
            "SELECT current_revision FROM pg_deliverable_keys "
            "WHERE deliverable_key=$1", deliverable_key)
        return int(row["current_revision"]) if row else 0

    async def review_with_revision_check(self, deliverable_id: str,
                                         expected_revision: int,
                                         action: str,
                                         reviewer: str = "") -> bool:
        """Revision-safe atomic review (C3/C4 contract)."""
        row = await self.db.fetch_one(
            "SELECT revision FROM pg_deliverables WHERE id=$1",
            deliverable_id)
        if row is None or int(row["revision"]) != expected_revision:
            return False
        updated = await self.db.fetch_one(
            "UPDATE pg_deliverables SET verification_json = "
            "verification_json || $2::jsonb, updated_at=now() "
            "WHERE id=$1 RETURNING id",
            deliverable_id,
            _json({"reviewed": action == "confirm", "reviewer": reviewer}))
        return updated is not None

    # ── publication jobs (interrupted publication recovery §21.20) ────
    async def create_publication_job(self, *, deliverable_id: str,
                                     job_id: str = "") -> str:
        jid = job_id or f"pub-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO pg_publication_jobs (id, deliverable_id)
               VALUES ($1,$2) ON CONFLICT (id) DO NOTHING""", jid, deliverable_id)
        return jid

    async def claim_publication_job(self, worker_id: str,
                                    lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            """WITH c AS (
                 SELECT id FROM pg_publication_jobs
                 WHERE state='pending' OR (state='running'
                       AND lease_expires_at < now())
                 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
               UPDATE pg_publication_jobs j
               SET state='running', lease_owner=$1,
                   lease_expires_at=now() + make_interval(secs => $2),
                   attempts=j.attempts+1, updated_at=now()
               FROM c WHERE j.id=c.id RETURNING *""", worker_id, lease_seconds)
        return dict(row) if row else None

    async def record_publication_progress(self, job_id: str, stage: str,
                                          state: str, error: str = "",
                                          completion_emitted: bool = False) -> None:
        await self.db.execute(
            "UPDATE pg_publication_jobs SET stage=$2, state=$3, error=$4, "
            "completion_emitted=$5, lease_owner=NULL, lease_expires_at=NULL, "
            "updated_at=now() WHERE id=$1", job_id, stage, state,
            error[:500], completion_emitted)

    async def publication_jobs_requiring_recovery(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """SELECT * FROM pg_publication_jobs
               WHERE state='running' AND lease_expires_at < now()
                  OR state='pending'""")
        return rows

    async def recover_publication(self, job: Dict[str, Any],
                                  completion_emitted: bool) -> None:
        await self.db.execute(
            "UPDATE pg_publication_jobs SET state='recovered', "
            "completion_emitted=$2, updated_at=now() WHERE id=$1",
            job["id"], completion_emitted)

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["content"] = _parse_json(out.get("content"))
        out["file_manifest"] = _parse_json(out.get("file_manifest"))
        out["verification"] = _parse_json(out.get("verification"))
        return out


# ═══════════════════════════════════════════════════════════════════════
# Feedback (§21.21) + notifications + settings + audit + idempotency
# + schedules + experiments + execution steps + usage
# ═══════════════════════════════════════════════════════════════════════

class PostgresFeedbackRepository(_Base):
    """Real-user feedback product journey (§21.21): submission with
    traceability + explicit diagnostic consent; authorized triage;
    retention/deletion. Feedback NEVER becomes a training reward here."""

    CATEGORIES = ("incorrect_result", "failed_action", "slow_run",
                  "missing_app", "confusing_ui", "other")
    TRIAGE = ("received", "investigating", "planned", "fixed", "closed")

    async def submit(self, feedback: Dict[str, Any]) -> str:
        fid = f"fb-{uuid.uuid4().hex[:14]}"
        if feedback.get("category") not in self.CATEGORIES:
            raise ValueError(
                f"category must be one of {self.CATEGORIES}")
        if not isinstance(feedback.get("helpful"), bool):
            raise ValueError("helpful must be a boolean")
        await self.db.execute(
            """INSERT INTO pg_feedback (id, user_id, helpful, category,
                 comment, expected_result, run_id, task_id, tool_id,
                 deliverable_revision, model_version, policy_version,
                 app_version, share_diagnostics)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            fid, feedback["user_id"], feedback["helpful"],
            feedback["category"], (feedback.get("comment") or "")[:4000],
            (feedback.get("expected_result") or "")[:2000],
            feedback.get("run_id"), feedback.get("task_id"),
            feedback.get("tool_id"), feedback.get("deliverable_revision"),
            feedback.get("model_version"), feedback.get("policy_version"),
            feedback.get("app_version", ""),
            bool(feedback.get("share_diagnostics", False)))
        return fid

    async def get(self, feedback_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_feedback WHERE id=$1", feedback_id)
        return dict(row) if row else None

    async def list_for_user(self, user_id: str,
                            limit: int = 50, *, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if run_id is not None:
            return await self.db.fetch_all(
                "SELECT * FROM pg_feedback WHERE user_id=$1 AND run_id=$2 "
                "ORDER BY created_at DESC LIMIT $3", user_id, run_id, limit)
        return await self.db.fetch_all(
            "SELECT * FROM pg_feedback WHERE user_id=$1 "
            "ORDER BY created_at DESC LIMIT $2", user_id, limit)

    async def list_for_triage(self, status: Optional[str] = None,
                              limit: int = 100) -> List[Dict[str, Any]]:
        if status:
            return await self.db.fetch_all(
                "SELECT * FROM pg_feedback WHERE triage_status=$1 "
                "ORDER BY created_at LIMIT $2", status, limit)
        return await self.db.fetch_all(
            "SELECT * FROM pg_feedback ORDER BY created_at LIMIT $1", limit)

    async def set_triage(self, feedback_id: str, status: str,
                         actor: str, note: str = "") -> bool:
        if status not in self.TRIAGE:
            raise ValueError(f"triage status must be one of {self.TRIAGE}")
        row = await self.db.fetch_one(
            "UPDATE pg_feedback SET triage_status=$2, triage_actor=$3, "
            "triage_note=$4, updated_at=now() WHERE id=$1 RETURNING id",
            feedback_id, status, actor, note[:2000])
        return row is not None

    async def delete_user_feedback(self, user_id: str, feedback_id: str) -> bool:
        row = await self.db.fetch_one(
            "DELETE FROM pg_feedback WHERE user_id=$1 AND id=$2 RETURNING id",
            user_id, feedback_id)
        return row is not None

    async def apply_retention(self, retention_days: int) -> int:
        rows = await self.db.fetch_all(
            "DELETE FROM pg_feedback WHERE created_at < now() - "
            "make_interval(days => $1) RETURNING id", retention_days)
        return len(rows)


class PostgresNotificationRepository(_Base):
    async def create(self, user_id: str, kind: str, title: str, body: str,
                     payload: Optional[Dict[str, Any]] = None) -> str:
        nid = f"ntf-{uuid.uuid4().hex[:14]}"
        await self.db.execute(
            """INSERT INTO pg_notifications (id, user_id, kind, title, body,
                 payload) VALUES ($1,$2,$3,$4,$5,$6::jsonb)""",
            nid, user_id, kind, title, body, _json(payload or {}))
        return nid

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM pg_notifications WHERE user_id=$1 "
            "ORDER BY created_at DESC LIMIT 100", user_id)

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_notifications SET read=TRUE "
            "WHERE user_id=$1 AND id=$2 RETURNING id", user_id, notification_id)
        return row is not None

    async def mark_all_read(self, user_id: str) -> int:
        row = await self.db.fetch_one(
            "WITH updated AS (UPDATE pg_notifications SET read=TRUE "
            "WHERE user_id=$1 AND read=FALSE RETURNING id) "
            "SELECT COUNT(*) AS cnt FROM updated", user_id)
        return (row["cnt"] if row else 0) or 0

    async def get_unread_count(self, user_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS cnt FROM pg_notifications "
            "WHERE user_id=$1 AND read=FALSE", user_id)
        return (row["cnt"] if row else 0) or 0


class PostgresSettingsRepository(_Base):
    async def get_settings(self, user_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT settings FROM pg_user_settings WHERE user_id=$1", user_id)
        return _parse_json(row["settings"]) if row else {}

    async def update_settings(self, user_id: str, settings: Dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO pg_user_settings (user_id, settings, updated_at)
               VALUES ($1,$2::jsonb, now())
               ON CONFLICT (user_id)
               DO UPDATE SET settings=$2::jsonb, updated_at=now()""",
            user_id, _json(settings))


class PostgresAuditRepository(_Base):
    async def log_event(self, event_data: Dict[str, Any]) -> None:
        """Same contract as SQLiteAuditRepository.log_event (dict with
        actor/action/target/detail keys of any naming)."""
        if hasattr(event_data, "model_dump"):
            event_data = event_data.model_dump(mode="json")
        actor = (event_data.get("actor_user_id")
                 or event_data.get("actor_id")
                 or event_data.get("user_id")
                 or event_data.get("actor"))
        action = (event_data.get("event_type")
                  or event_data.get("action") or "unknown")
        target = str(event_data.get("target")
                     or event_data.get("resource_id")
                     or event_data.get("resource") or "")
        detail = {k: v for k, v in event_data.items()
                  if k not in ("actor_user_id", "actor_id", "user_id",
                               "actor", "action", "event_type", "target",
                               "resource", "resource_id")}
        await self.db.execute(
            """INSERT INTO pg_audit_events (actor_user_id, action, target,
                 detail) VALUES ($1,$2,$3,$4::jsonb)""",
            actor, str(action), target, _json(detail))

    async def record(self, actor_user_id: Optional[str], action: str,
                     target: str = "",
                     detail: Optional[Dict[str, Any]] = None) -> None:
        await self.log_event({"actor_user_id": actor_user_id,
                              "action": action, "target": target,
                              "detail": detail or {}})


class PostgresIdempotencyRepository(_Base):
    """Full execution-dedup ledger contract (§32.6 store parity).

    Mirrors SQLiteIdempotencyRepository semantics on migration 0027's
    expanded pg_idempotency table: atomic claim with paused-reclaim,
    terminal/uncertain commits, stale-running quarantine and the
    conditional one-winner uncertain reconciliation. The legacy
    get_or_put cache is preserved on top of the same table (its rows
    are terminal 'completed' entries, never live locks)."""

    async def get_or_put(self, key: str, scope: str,
                         factory) -> Tuple[Any, bool]:
        row = await self.db.fetch_one(
            "SELECT response FROM pg_idempotency WHERE key=$1", key)
        if row:
            return _parse_json(row["response"]), False
        value = await factory()
        await self.db.execute(
            """INSERT INTO pg_idempotency (key, scope, response, status)
               VALUES ($1,$2,$3::jsonb,'completed')
               ON CONFLICT (key) DO NOTHING""",
            key, scope, _json(value))
        return value, True

    async def atomic_insert(self, key: str, workflow_id: str,
                            task_id: str) -> bool:
        """Claim the execution lock: INSERT wins on a fresh key or on a
        PAUSED entry (human-paused calls are reclaimable); a running /
        completed / failed / uncertain entry keeps its owner."""
        row = await self.db.fetch_one(
            """INSERT INTO pg_idempotency
                 (key, workflow_id, task_id, status)
               VALUES ($1,$2,$3,'running')
               ON CONFLICT (key) DO UPDATE
                 SET status='running', workflow_id=$2, task_id=$3,
                     updated_at=now()
               WHERE pg_idempotency.status='paused'
               RETURNING key""",
            key, workflow_id, task_id)
        return row is not None

    async def get_entry(self, key: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT workflow_id, task_id, status, result_payload "
            "FROM pg_idempotency WHERE key=$1", key)
        if not row:
            return None
        return {"workflow_id": row["workflow_id"],
                "task_id": row["task_id"],
                "status": row["status"],
                "result_payload": row["result_payload"]}

    async def get_record(self, key: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT key, workflow_id, task_id, status, result_payload, "
            "created_at, updated_at FROM pg_idempotency WHERE key=$1", key)
        return dict(row) if row else None

    async def release_replay_key(self, key: str) -> bool:
        """§4.16: release a replay identity only when its command never
        reached a terminal/accepted state."""
        row = await self.db.fetch_one(
            "DELETE FROM pg_idempotency WHERE key=$1 "
            "AND status IN ('running','paused') RETURNING key", key)
        return row is not None

    async def commit_success(self, key: str, result_payload: str) -> None:
        await self.db.execute(
            "UPDATE pg_idempotency SET status='completed', "
            "result_payload=$2, updated_at=now() WHERE key=$1",
            key, result_payload)

    async def commit_failure(self, key: str) -> None:
        await self.db.execute(
            "UPDATE pg_idempotency SET status='failed', updated_at=now() "
            "WHERE key=$1", key)

    async def commit_pause(self, key: str) -> None:
        await self.db.execute(
            "UPDATE pg_idempotency SET status='paused' "
            "WHERE key=$1 AND status='running'", key)

    async def commit_uncertain(self, key: str) -> None:
        await self.db.execute(
            "UPDATE pg_idempotency SET status='uncertain', "
            "updated_at=now() WHERE key=$1 AND status='running'", key)

    async def sweep_stale_running(self, older_than_seconds: int = 1800) -> int:
        """Quarantine 'running' entries past the threshold as 'uncertain'
        (audit F07) — never 'failed', which would invite a blind replay."""
        row = await self.db.fetch_one(
            """WITH swept AS (
                 UPDATE pg_idempotency SET status='uncertain', updated_at=now()
                 WHERE status='running'
                   AND updated_at < now() - make_interval(secs => $1)
                 RETURNING key)
               SELECT count(*)::int AS n FROM swept""", older_than_seconds)
        return int(row["n"]) if row else 0

    async def reconcile_uncertain(self, key: str, outcome: str,
                                  result_payload: Optional[str] = None) -> None:
        """§5.11 one-winner reconciliation: the CONDITIONAL update
        (`WHERE status='uncertain'`) lets exactly one resolver commit —
        the loser raises instead of overwriting history."""
        if outcome not in ("completed", "failed"):
            raise ValueError("outcome must be 'completed' or 'failed'")
        existing = await self.db.fetch_one(
            "SELECT status FROM pg_idempotency WHERE key=$1", key)
        if not existing or existing["status"] != "uncertain":
            raise ValueError(
                "Only 'uncertain' ledger entries can be reconciled")
        new_status = "completed" if outcome == "completed" else "failed"
        row = await self.db.fetch_one(
            """UPDATE pg_idempotency SET status=$2, result_payload=$3,
                 updated_at=now()
               WHERE key=$1 AND status='uncertain' RETURNING key""",
            key, new_status, result_payload or "{}")
        if not row:
            raise ValueError(
                "Concurrent resolution — the entry is no longer uncertain")

    async def reset_failed_task(self, workflow_id: str, task_id: str) -> None:
        """Explicit retries release only failures — never completed
        effects or live locks."""
        await self.db.execute(
            "UPDATE pg_idempotency SET status='paused' "
            "WHERE workflow_id=$1 AND task_id=$2 AND status='failed'",
            workflow_id, task_id)


class PostgresScheduleRepository(_Base):
    async def create_schedule(self, schedule: Dict[str, Any]) -> str:
        sid = schedule.get("id") or f"sched-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO pg_schedules (id, user_id, name, cron, goal)
               VALUES ($1,$2,$3,$4,$5)""", sid, schedule.get("user_id", ""),
            schedule.get("name", ""), schedule["cron"],
            schedule.get("goal", ""))
        return sid

    async def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM pg_schedules WHERE user_id=$1 ORDER BY created_at",
            user_id)

    async def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        row = await self.db.fetch_one(
            "UPDATE pg_schedules SET enabled=$2 WHERE id=$1 RETURNING id",
            schedule_id, enabled)
        return row is not None


class PostgresExperimentRepository(_Base):
    async def create_experiment(self, experiment) -> None:
        await self.db.execute(
            """INSERT INTO pg_experiments (id, kind, target, hypothesis,
                 expected_benefit, baseline_ref, candidate_ref, dataset_ref,
                 dataset_hash, grading_hash, thresholds, manifest,
                 proposed_by)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,
                       $12::jsonb,$13)
               ON CONFLICT (id) DO NOTHING""",
            experiment.experiment_id, experiment.kind.value
            if hasattr(experiment.kind, "value") else experiment.kind,
            experiment.target, experiment.hypothesis,
            experiment.expected_benefit, experiment.baseline_ref,
            experiment.candidate_ref, experiment.dataset_ref,
            experiment.dataset_hash, experiment.grading_hash,
            experiment.thresholds_json, experiment.manifest_json,
            experiment.proposed_by)

    async def get_experiment(self, experiment_id: str):
        from backend.models.experiment import Experiment, ExperimentState, ExperimentKind
        row = await self.db.fetch_one(
            "SELECT * FROM pg_experiments WHERE id=$1", experiment_id)
        if not row:
            return None
        data = dict(row)
        data["state"] = ExperimentState(data["state"])
        data["kind"] = ExperimentKind(data["kind"])
        return Experiment(**{k: v for k, v in data.items()
                             if k in Experiment.model_fields})

    async def transition(self, experiment_id: str, state, *, role: str,
                         actor: str, actor_field: str) -> Dict[str, Any]:
        await self.db.execute(
            "UPDATE pg_experiments SET state=$2, updated_at=now() "
            "WHERE id=$1", experiment_id,
            state.value if hasattr(state, "value") else state)
        if actor_field:
            await self.db.execute(
                f"UPDATE pg_experiments SET {actor_field}=$2 WHERE id=$1",
                experiment_id, actor)
        return await self.get_experiment(experiment_id)

    async def save_results(self, experiment_id: str,
                           results_json: str) -> None:
        await self.db.execute(
            "UPDATE pg_experiments SET results=$2::jsonb, updated_at=now() "
            "WHERE id=$1", experiment_id, results_json)


class PostgresExecutionStepRepository(_Base):
    async def create_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        sid = step.get("id") or f"step-{uuid.uuid4().hex[:14]}"
        await self.db.execute(
            """INSERT INTO pg_execution_steps (id, run_id, task_id, sequence,
                 state, action, observation, evidence)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb)""",
            sid, step["run_id"], step["task_id"],
            step.get("sequence", 0), step.get("state", "OBSERVING"),
            step.get("action", ""), _json(step.get("observation", {})),
            _json(step.get("evidence", {})))
        return await self.get_step(sid)

    async def get_step(self, step_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_execution_steps WHERE id=$1", step_id)
        return self._shape(row) if row else None

    async def steps_for_task(self, run_id: str, task_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_execution_steps WHERE run_id=$1 AND task_id=$2 "
            "ORDER BY sequence", run_id, task_id)
        return [self._shape(r) for r in rows]

    async def steps_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_execution_steps WHERE run_id=$1 ORDER BY sequence",
            run_id)
        return [self._shape(r) for r in rows]

    @staticmethod
    def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        out["observation"] = _parse_json(out.get("observation"))
        out["evidence"] = _parse_json(out.get("evidence"))
        return out


class PostgresUsageRepository(_Base):
    async def add_usage(self, user_id: str, day: str, tokens: int,
                        cost_usd: float = 0.0) -> None:
        await self.db.execute(
            """INSERT INTO pg_usage_daily (user_id, day, tokens, cost_usd)
               VALUES ($1,$2,$3,$4) ON CONFLICT (user_id, day)
               DO UPDATE SET tokens = pg_usage_daily.tokens + $3,
                 cost_usd = pg_usage_daily.cost_usd + $4""",
            user_id, day, tokens, cost_usd)

    async def get_usage(self, user_id: str, day: str) -> int:
        row = await self.db.fetch_one(
            "SELECT tokens FROM pg_usage_daily WHERE user_id=$1 AND day=$2",
            user_id, day)
        return int(row["tokens"]) if row else 0

    async def usage_summary_for_run(self, run_id: str) -> Dict[str, Any]:
        # Per-run accounting rides the budget_reservations table (0001).
        row = await self.db.fetch_one(
            "SELECT COALESCE(SUM(settled),0) AS settled FROM "
            "budget_reservations WHERE run_id=$1", run_id)
        return {"settled": int(row["settled"]) if row else 0}


class PostgresPluginRepository(_Base):
    async def save(self, record) -> None:
        await self.db.execute(
            """INSERT INTO pg_plugins (id, manifest, state, installed_by,
                 installed_at, updated_at)
               VALUES ($1,$2::jsonb,$3,$4,$5,$6)
               ON CONFLICT (id) DO UPDATE SET manifest=EXCLUDED.manifest,
                 state=EXCLUDED.state, updated_at=now()""",
            record.id, record.manifest.model_dump_json(),
            record.state.value, record.installed_by,
            record.installed_at, record.updated_at)

    async def get_by_id(self, record_id: str):
        from backend.models.plugin import PluginRecord
        import json as _json
        row = await self.db.fetch_one(
            "SELECT * FROM pg_plugins WHERE id=$1", record_id)
        if not row:
            return None
        return PluginRecord(id=row["id"],
                            manifest=_json.loads(row["manifest"]),
                            state=row["state"],
                            installed_by=row["installed_by"],
                            installed_at=row["installed_at"],
                            updated_at=row["updated_at"])

    async def list_all(self, user_id: str = None):
        from backend.models.plugin import PluginRecord
        import json as _json
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_plugins ORDER BY installed_at")
        records = [PluginRecord(id=r["id"], manifest=_json.loads(r["manifest"]),
                                state=r["state"],
                                installed_by=r["installed_by"],
                                installed_at=r["installed_at"],
                                updated_at=r["updated_at"]) for r in rows]
        return [r for r in records if not user_id
                or r.manifest.scope.value != "USER"
                or r.installed_by == user_id]


class PostgresContentRepository(_Base):
    async def create(self, kind: str, payload: dict) -> dict:
        """SQLite/Firestore contract parity (contact + blog records):
        store an id-stamped copy of the payload and return the record.
        Anonymous records (e.g. contact submissions) use the 'anonymous'
        sentinel user."""
        record = {**payload, "id": str(uuid.uuid4()),
                  "created_at": datetime.now(timezone.utc).isoformat()}
        await self.db.execute(
            """INSERT INTO pg_content (id, user_id, kind, content)
               VALUES ($1, $2, $3, $4::jsonb)
               ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content,
                 updated_at=now()""",
            record["id"], payload.get("user_id") or "anonymous",
            kind, _json(record))
        return record

    async def save(self, content_id: str, user_id: str, kind: str,
                   content: Dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO pg_content (id, user_id, kind, content)
               VALUES ($1,$2,$3,$4::jsonb)
               ON CONFLICT (id) DO UPDATE SET content=EXCLUDED.content,
                 updated_at=now()""",
            content_id, user_id, kind, _json(content))

    async def get(self, content_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_content WHERE id=$1", content_id)
        if not row:
            return None
        out = dict(row)
        out["content"] = _parse_json(out.get("content"))
        return out

    async def list_for_user(self, user_id: str, kind: str = None) -> List[Dict[str, Any]]:
        if kind:
            return await self.db.fetch_all(
                "SELECT * FROM pg_content WHERE user_id=$1 AND kind=$2 "
                "ORDER BY created_at DESC", user_id, kind)
        return await self.db.fetch_all(
            "SELECT * FROM pg_content WHERE user_id=$1 "
            "ORDER BY created_at DESC", user_id)

    async def delete(self, content_id: str, user_id: str) -> bool:
        row = await self.db.fetch_one(
            "DELETE FROM pg_content WHERE id=$1 AND user_id=$2 RETURNING id",
            content_id, user_id)
        return row is not None


class PostgresAdaptivePolicyRepository(_Base):
    async def record_outcome(self, model: str, success: bool,
                             latency_ms: float) -> None:
        await self.db.execute(
            """INSERT INTO pg_adaptive_policy (context_key, model, successes,
                 failures, total_latency_ms)
               VALUES ('global', $2, $3, $4, $5)
               ON CONFLICT (context_key, model) DO UPDATE SET
                 successes = pg_adaptive_policy.successes + $3,
                 failures = pg_adaptive_policy.failures + $4,
                 total_latency_ms = pg_adaptive_policy.total_latency_ms + $5,
                 updated_at = now()""",
            "global", model, 1 if success else 0,
            0 if success else 1, latency_ms)

    async def get_stats(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT model, successes, failures, total_latency_ms "
            "FROM pg_adaptive_policy WHERE context_key='global'")
        return [dict(r) for r in rows]


class PostgresScheduleEngineRepository(_Base):
    async def create_schedule(self, schedule_data: Dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO pg_schedule_full (id, user_id, name, cron, goal,
                 payload) VALUES ($1,$2,$3,$4,$5,$6::jsonb)
               ON CONFLICT (id) DO NOTHING""",
            schedule_data.get("id") or f"sched-{uuid.uuid4().hex[:12]}",
            schedule_data.get("user_id", ""), schedule_data.get("name", ""),
            schedule_data.get("cron", ""), schedule_data.get("goal", ""),
            _json(schedule_data))

    async def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM pg_schedule_full WHERE id=$1", schedule_id)
        return self._shape(row) if row else None

    async def list_schedules(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_schedule_full WHERE user_id=$1 "
            "ORDER BY created_at", user_id)
        return [self._shape(r) for r in rows]

    async def update_schedule(self, schedule_id: str,
                              updates: Dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE pg_schedule_full SET payload = payload || $2::jsonb, "
            "updated_at = COALESCE(updated_at, now()) WHERE id=$1",
            schedule_id, _json(updates))

    async def delete_schedule(self, schedule_id: str) -> bool:
        row = await self.db.fetch_one(
            "DELETE FROM pg_schedule_full WHERE id=$1 RETURNING id",
            schedule_id)
        return row is not None

    async def list_due_schedules(self, now_iso: str) -> List[Dict[str, Any]]:
        # Accept iso strings (the worker's contract) OR datetimes.
        when = now_iso if isinstance(now_iso, datetime) \
            else datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        rows = await self.db.fetch_all(
            "SELECT * FROM pg_schedule_full WHERE enabled AND "
            "(payload->>'next_run_at')::timestamptz <= $1", when)
        return [self._shape(r) for r in rows]

    @staticmethod
    def _shape(row):
        out = dict(row)
        out["payload"] = _parse_json(out.get("payload"))
        return out


class PostgresBudgetRepository(_Base):
    """FULL §2 budget accounting on Postgres: bucket state + the
    reserve→dispatch→settle/release/uncertain state machine. Same
    contracts as SQLiteUsageRepository so admission/metering run
    unchanged on the multi-process profile."""

    def __init__(self, db: PostgresDatabaseManager):
        super().__init__(db)

    # ── buckets ────────────────────────────────────────────────────────
    async def _bucket(self, conn, scope, subject_id, period,
                      unit="tokens") -> Dict[str, int]:
        row = await conn.fetchrow(
            "SELECT * FROM budget_buckets WHERE scope=$1 AND subject_id=$2 "
            "AND period=$3 AND unit=$4", scope, subject_id, period, unit)
        if row is None:
            return {"consumed": 0, "reserved": 0, "uncertain_reserved": 0}
        return {"consumed": int(row["consumed"]),
                "reserved": int(row["reserved"]),
                "uncertain_reserved": int(row["uncertain_reserved"])}

    @staticmethod
    def _bump_sql(delta_consumed=0, delta_reserved=0,
                  delta_uncertain=0, limit_applied=None) -> str:
        return (
            f"""INSERT INTO budget_buckets (scope, subject_id, period, unit,
                  consumed, reserved, uncertain_reserved, limit_applied)
                VALUES ($1,$2,$3,$4,
                        GREATEST(0, {delta_consumed}),
                        GREATEST(0, {delta_reserved}),
                        GREATEST(0, {delta_uncertain}), $5)
                ON CONFLICT (scope, subject_id, period, unit) DO UPDATE SET
                  consumed = GREATEST(0, budget_buckets.consumed + ({delta_consumed})),
                  reserved = GREATEST(0, budget_buckets.reserved + ({delta_reserved})),
                  uncertain_reserved = GREATEST(0, budget_buckets.uncertain_reserved + ({delta_uncertain})),
                  limit_applied = COALESCE($5, budget_buckets.limit_applied),
                  updated_at = now()""")

    async def get_budget_snapshot(self, scope, subject_id, period, limit,
                                  unit=None):
        from backend.models.usage import BudgetScope, BudgetUnit, BudgetSnapshot
        unit = unit or BudgetUnit.TOKENS
        unit_val = unit.value if hasattr(unit, "value") else str(unit)
        scope_val = scope.value if hasattr(scope, "value") else str(scope)
        row = await self.db.fetch_one(
            "SELECT * FROM budget_buckets WHERE scope=$1 AND subject_id=$2 "
            "AND period=$3 AND unit=$4", scope_val, subject_id, period,
            unit_val)
        consumed = int(row["consumed"]) if row else 0
        reserved = int(row["reserved"]) if row else 0
        uncertain = int(row["uncertain_reserved"]) if row else 0
        return BudgetSnapshot(
            scope=scope, subject_id=subject_id, unit=unit, period=period,
            limit=int(limit), consumed=consumed, reserved=reserved,
            uncertain_reserved=uncertain,
            available=max(0, int(limit) - consumed - reserved - uncertain),
            accounting_status="healthy")

    # ── the reservation state machine (§2.7) ───────────────────────────
    async def reserve(self, request, buckets) -> Any:
        from backend.models.usage import (
            Reservation, ReservationRequest, ReservationState, BudgetUnit)
        # SAME semantics as SQLiteUsageRepository: the model's
        # effective_request_key() (deterministic per logical call) +
        # fingerprint(); without a call identity, every invocation is
        # its own attempt (§2.5).
        import hashlib
        if hasattr(request, "effective_request_key"):
            request_key = request.effective_request_key()
            digest = request.fingerprint()
        else:
            digest = hashlib.sha256(
                json.dumps({
                    "user_id": request.user_id,
                    "units": request.units,
                    "unit": str(getattr(request, "unit", "tokens")),
                    "provider": getattr(request, "provider", ""),
                    "model": getattr(request, "model", ""),
                    "task_id": getattr(request, "task_id", ""),
                }, sort_keys=True, default=str).encode()).hexdigest()
            request_key = f"res-{digest[:24]}"
        from backend.models.usage import BudgetError, BudgetErrorCode
        if request.units <= 0 or not buckets:
            raise BudgetError(BudgetErrorCode.BUDGET_POLICY_INVALID,
                              "positive units and at least one budget bucket required")
        rid = f"res_{uuid.uuid4().hex[:24]}"
        period = getattr(request, "period", None) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        unit = getattr(request, "unit", BudgetUnit.TOKENS)
        unit_value = getattr(unit, "value", unit)
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1))", request_key)
                existing = await conn.fetchrow("SELECT * FROM budget_reservations WHERE request_key=$1",
                                               request_key)
                if existing:
                    if existing["request_digest"] != digest:
                        raise BudgetError(BudgetErrorCode.RESERVATION_CONFLICT,
                                          "request key reused with a different payload")
                    return self._row_to_reservation(existing)
                charged = []
                for bucket in sorted(buckets, key=lambda item: (item.scope, item.subject_id)):
                    scope_val = getattr(bucket.scope, "value", bucket.scope)
                    await conn.execute("""INSERT INTO budget_buckets
                        (scope,subject_id,period,unit,limit_applied) VALUES ($1,$2,$3,$4,$5)
                        ON CONFLICT (scope,subject_id,period,unit) DO NOTHING""",
                        scope_val, bucket.subject_id, period, unit_value, bucket.limit)
                    accepted = await conn.fetchval("""UPDATE budget_buckets SET
                        reserved=reserved+$5,limit_applied=$6,updated_at=now()
                        WHERE scope=$1 AND subject_id=$2 AND period=$3 AND unit=$4
                        AND consumed+reserved+$5 <= $6 RETURNING reserved""",
                        scope_val, bucket.subject_id, period, unit_value, request.units, bucket.limit)
                    if accepted is None:
                        raise BudgetError(BudgetErrorCode.BUDGET_EXHAUSTED,
                                          f"budget exhausted for scope={scope_val}", required=request.units)
                    charged.append({"scope": scope_val,
                                    "subject_id": bucket.subject_id,
                                    "period": period, "units": request.units})
                await conn.execute(
                    """INSERT INTO budget_reservations (id, reservation_id,
                         request_key, request_digest, user_id,
                         organization_id, run_id, task_id, call_id,
                         attempt_id, provider, model, unit, units, state,
                         period, buckets_json)
                       VALUES ($1,$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                               'reserved',$14,$15::jsonb)""",
                    # `id` is the 0001-era PK (still NOT NULL): carry the
                    # runtime reservation identity into it so both writers
                    # share one row per reservation.
                    rid, request_key, digest, request.user_id,
                    getattr(request, "organization_id", None),
                    getattr(request, "run_id", None),
                    getattr(request, "task_id", None),
                    getattr(request, "call_id", None),
                    getattr(request, "attempt_id", None),
                    getattr(request, "provider", "gemini"),
                    getattr(request, "model", None), unit_value,
                    request.units, period, _json(charged))
        return await self.get_reservation(rid)

    async def mark_dispatching(self, reservation_id: str,
                               expected_version: Optional[int] = None) -> Any:
        """Full §2.6 contract (parity with SQLiteUsageRepository): the
        optimistic `expected_version` guard makes a concurrent double
        dispatch visible instead of silently overwriting state."""
        row = await self.db.fetch_one(
            """UPDATE budget_reservations SET state='dispatching',
               dispatch_started_at=now(), updated_at=now(),
               record_version = record_version + 1
               WHERE reservation_id=$1 AND state='reserved'
                 AND ($2::int IS NULL OR record_version=$2)
               RETURNING *""",
            reservation_id, expected_version)
        if row is None:
            raise ValueError(
                f"reservation {reservation_id}: cannot mark dispatching "
                "(not in reserved state)")
        return self._row_to_reservation(row)

    async def settle(self, reservation_id: str, receipt) -> Any:
        from backend.models.usage import ReservationState, BudgetError, \
            BudgetErrorCode
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM budget_reservations WHERE "
                    "reservation_id=$1 FOR UPDATE", reservation_id)
                if row is None:
                    raise ValueError(f"reservation {reservation_id} missing")
                # §2.7 state machine: a settled reservation cannot settle
                # again (double-counted spend); a released one cannot
                # settle at all — both raise, like SQLiteUsageRepository.
                if row["state"] == ReservationState.SETTLED.value:
                    raise BudgetError(
                        BudgetErrorCode.SETTLEMENT_CONFLICT,
                        f"reservation {reservation_id} is already settled")
                if row["state"] == ReservationState.RELEASED.value:
                    raise BudgetError(
                        BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                        f"reservation {reservation_id} was released; "
                        "settlement goes through the correction path")
                units = int(row["units"])
                # §2.7/30.8 parity with SQLite: a receipt ABOVE the
                # reservation settles the overrun (never clipped); the
                # overrun is visible on the settled event for reconciliation.
                settled = max(0, int(receipt.units))
                buckets = _parse_json(row["buckets_json"]) or []
                for bucket in buckets:
                    await conn.execute(
                        """UPDATE budget_buckets SET
                             reserved = GREATEST(0, reserved - $4),
                             consumed = consumed + $5,
                             updated_at = now()
                           WHERE scope=$1 AND subject_id=$2 AND period=$3
                             AND unit='tokens'""",
                        bucket["scope"], bucket["subject_id"],
                        bucket["period"], units, settled)
                await conn.execute(
                    """UPDATE budget_reservations SET state='settled',
                       settled_units=$2, provider_request_id=$3,
                       pricing_version=$4, updated_at=now()
                       WHERE reservation_id=$1""",
                    reservation_id, settled,
                    getattr(receipt, "provider_request_id", None),
                    getattr(receipt, "pricing_version", None))
        return await self.get_reservation(reservation_id)

    async def release_unused(self, reservation_id: str,
                             evidence: Optional[Dict[str, Any]] = None,
                             reason: str = "") -> Any:
        """§2.7 contract parity: callers pass `evidence` (SQLite signature);
        the evidence digest lands in receipt_digest for the audit trail."""
        evidence_digest = None
        if evidence:
            evidence_digest = hashlib.sha256(
                json.dumps(evidence, sort_keys=True, default=str)
                .encode()).hexdigest()
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM budget_reservations WHERE "
                    "reservation_id=$1 AND state IN ('reserved','dispatching') "
                    "FOR UPDATE", reservation_id)
                if row is None:
                    return await self.get_reservation(reservation_id)
                units = int(row["units"])
                for bucket in (_parse_json(row["buckets_json"]) or []):
                    await conn.execute(
                        """UPDATE budget_buckets SET
                             reserved = GREATEST(0, reserved - $4),
                             updated_at = now()
                           WHERE scope=$1 AND subject_id=$2 AND period=$3
                             AND unit='tokens'""",
                        bucket["scope"], bucket["subject_id"],
                        bucket["period"], units)
                await conn.execute(
                    """UPDATE budget_reservations SET state='released',
                       receipt_digest=COALESCE($2, receipt_digest),
                       updated_at=now() WHERE reservation_id=$1""",
                    reservation_id, evidence_digest)
        return await self.get_reservation(reservation_id)

    async def mark_uncertain(self, reservation_id: str, reason: str) -> Any:
        """§2.7 contract parity with SQLite: allowed FROM reserved OR
        dispatching; other states raise a conflict (never a silent no-op)."""
        from backend.models.usage import BudgetError, BudgetErrorCode, \
            ReservationState
        async with self.db.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM budget_reservations WHERE "
                    "reservation_id=$1 FOR UPDATE", reservation_id)
                if row is None:
                    raise BudgetError(
                        BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                        f"reservation {reservation_id} missing")
                state = row["state"]
                if state == ReservationState.UNCERTAIN.value:
                    return await self.get_reservation(reservation_id)
                if state not in (ReservationState.RESERVED.value,
                                 ReservationState.DISPATCHING.value):
                    raise BudgetError(
                        BudgetErrorCode.RESERVATION_STATE_CONFLICT,
                        f"cannot mark a reservation in state {state} uncertain")
                units = int(row["units"])
                for bucket in (_parse_json(row["buckets_json"]) or []):
                    # §8 of the budget spec: uncertain_reserved is a SUBSET
                    # of reserved (BudgetSnapshot clamps it). The hold STAYS
                    # in reserved; uncertain only increments.
                    await conn.execute(
                        """UPDATE budget_buckets SET
                             uncertain_reserved = uncertain_reserved + $4,
                             updated_at = now()
                           WHERE scope=$1 AND subject_id=$2 AND period=$3
                             AND unit='tokens'""",
                        bucket["scope"], bucket["subject_id"],
                        bucket["period"], units)
                await conn.execute(
                    """UPDATE budget_reservations SET state='uncertain',
                       receipt_digest=$2, updated_at=now()
                       WHERE reservation_id=$1""",
                    reservation_id,
                    hashlib.sha256(reason.encode()).hexdigest())
        return await self.get_reservation(reservation_id)

    async def get_reservation(self, reservation_id: str):
        row = await self.db.fetch_one(
            "SELECT * FROM budget_reservations WHERE reservation_id=$1",
            reservation_id)
        return self._row_to_reservation(row) if row else None

    def _row_to_reservation(self, row):
        from backend.models.usage import Reservation, ReservationState
        return Reservation(
            reservation_id=row["reservation_id"],
            request_key=row["request_key"],
            request_digest=row["request_digest"],
            user_id=row["user_id"],
            units=int(row["units"]),
            state=ReservationState(row["state"]),
            organization_id=row.get("organization_id"),
            run_id=row.get("run_id"),
            task_id=row.get("task_id"),
            call_id=row.get("call_id"),
            attempt_id=row.get("attempt_id"),
            provider=row.get("provider") or "gemini",
            model=row.get("model"),
            period=row.get("period"),
            created_at=row["created_at"].isoformat() if row.get("created_at") else None,
            updated_at=row["updated_at"].isoformat() if row.get("updated_at") else None,
        )

    async def add_usage(self, user_id: str, day: str, tokens: int) -> None:
        await self.db.execute(
            """INSERT INTO pg_usage_daily (user_id, day, tokens)
               VALUES ($1,$2,$3) ON CONFLICT (user_id, day)
               DO UPDATE SET tokens = pg_usage_daily.tokens + $3""",
            user_id, day, tokens)

    async def get_usage(self, user_id: str, day: str) -> int:
        row = await self.db.fetch_one(
            "SELECT tokens FROM pg_usage_daily WHERE user_id=$1 AND day=$2",
            user_id, day)
        return int(row["tokens"]) if row else 0

    async def usage_summary_for_run(self, run_id: str) -> Dict[str, Any]:
        row = await self.db.fetch_one(
            "SELECT COALESCE(SUM(settled_units),0) AS settled "
            "FROM budget_reservations WHERE run_id=$1", run_id)
        return {"settled": int(row["settled"]) if row else 0}


class PostgresWaitRepository(_Base):
    """§4.13 parent-wait contract on Postgres (same methods as
    SQLiteWaitRepository)."""

    async def create_wait(self, parent_run_id: str, parent_task_id: str,
                          child_command_id: str, child_run_id: str,
                          condition: str = "child_build_succeeded",
                          wait_id: str = "", created_at: str = "") -> str:
        wid = wait_id or f"wait-{uuid.uuid4().hex[:12]}"
        await self.db.execute(
            """INSERT INTO parent_waits (wait_id, parent_run_id,
                 parent_task_id, child_command_id, child_run_id, condition,
                 state, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,'waiting',$7)
               ON CONFLICT (wait_id) DO NOTHING""",
            wid, parent_run_id, parent_task_id, child_command_id,
            child_run_id, condition, created_at or str(int(time.time())))
        return wid

    async def resolve_wait(self, wait_id: str, outcome: str,
                           resolved_at: str = "") -> bool:
        row = await self.db.fetch_one(
            "UPDATE parent_waits SET state=$2 "
            "WHERE wait_id=$1 AND state='waiting' RETURNING wait_id",
            wait_id, f"resolved:{outcome}" if outcome == "resolved"
            else f"failed:{outcome}")
        return row is not None

    async def find_open_wait(self, parent_run_id: str,
                             parent_task_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT * FROM parent_waits WHERE parent_run_id=$1 "
            "AND parent_task_id=$2 AND state='waiting' "
            "ORDER BY created_at DESC LIMIT 1", parent_run_id, parent_task_id)
        return dict(row) if row else None

    async def unresolved_waits_for_child(self, child_run_id: str) -> list:
        return await self.db.fetch_all(
            "SELECT * FROM parent_waits WHERE child_run_id=$1 "
            "AND state='waiting'", child_run_id)

    async def unresolved_waits(self, limit: int = 50) -> list:
        return await self.db.fetch_all(
            "SELECT * FROM parent_waits WHERE state='waiting' "
            "ORDER BY created_at LIMIT $1", limit)

    async def latest_wait(self, run_id, task_id):
        return await self.db.fetch_one("SELECT * FROM parent_waits WHERE parent_run_id=$1 AND parent_task_id=$2 "
                                       "ORDER BY created_at DESC LIMIT 1", run_id, task_id)

    async def resolve_with_wake(self, wait, payload, *, outcome="resolved"):
        if outcome not in ("resolved", "failed"):
            raise ValueError("Unsupported parent-wait outcome")
        now = int(time.time())
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("UPDATE parent_waits SET state=$2 WHERE wait_id=$1 "
                                      "AND state='waiting' RETURNING wait_id", wait["wait_id"], outcome)
            if not row:
                return False
            await conn.execute("INSERT INTO pg_outbox_v2(event_id,aggregate_id,aggregate_version,event_type,"
                               "payload_json,created_at,available_at) VALUES($1,$2,1,'parent.ready',$3::jsonb,$4,$4)",
                               "wake-" + wait["wait_id"], wait["parent_run_id"], json.dumps(payload), now)
        return True

    async def reconcile_resolved_children(self, workflow_repo,
                                          limit: int = 50) -> int:
        """Waits whose child run reached a terminal state resolve once."""
        resolved = 0
        for wait in await self.unresolved_waits(limit=limit):
            child = None
            if hasattr(workflow_repo, "get_run"):
                child = await workflow_repo.get_run(wait["child_run_id"])
            if not child:
                continue
            status = (child.get("status") or "").upper()
            if status in ("COMPLETED", "FAILED", "CANCELLED"):
                outcome = "resolved" if status == "COMPLETED" else "failed"
                if await self.resolve_wait(wait["wait_id"], outcome):
                    resolved += 1
        return resolved
