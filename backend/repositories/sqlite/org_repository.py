"""
AgentOS — SQLite Org Repository

Orgs and membership for the enterprise RBAC foundation. Roles:
owner > admin > member > viewer. The owner's membership is immutable
(cannot be removed or demoted) to keep the accountability chain intact.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.repositories.base import BaseOrgRepository
from backend.repositories.sqlite.database import DatabaseManager

logger = logging.getLogger(__name__)

OWNER_ROLE = "owner"


class SQLiteOrgRepository(BaseOrgRepository):

    def __init__(self, db: DatabaseManager):
        self.db = db

    async def create_org(self, name: str, owner_id: str) -> Dict[str, Any]:
        org_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT INTO orgs (org_id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (org_id, name, owner_id, now),
        )
        await self.db.execute(
            "INSERT INTO org_members (org_id, user_id, role, added_at, added_by) VALUES (?, ?, ?, ?, ?)",
            (org_id, owner_id, OWNER_ROLE, now, owner_id),
        )
        await self.db.commit()
        return {"org_id": org_id, "name": name, "owner_id": owner_id, "created_at": now}

    async def get_org(self, org_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one("SELECT * FROM orgs WHERE org_id = ?", (org_id,))
        return dict(row) if row else None

    async def list_orgs_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT o.org_id, o.name, o.owner_id, o.created_at, m.role
            FROM orgs o JOIN org_members m ON m.org_id = o.org_id
            WHERE m.user_id = ?
            ORDER BY o.created_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def get_member_role(self, org_id: str, user_id: str) -> Optional[str]:
        row = await self.db.fetch_one(
            "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        return row["role"] if row else None

    async def list_members(self, org_id: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            """
            SELECT m.user_id, m.role, m.added_at, u.email, u.name
            FROM org_members m LEFT JOIN users u ON u.id = m.user_id
            WHERE m.org_id = ?
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, m.added_at
            """,
            (org_id,),
        )
        return [dict(r) for r in rows]

    # ── Invitation lifecycle (audit F01): membership requires acceptance ──

    async def create_invitation(self, org_id: str, email: str, role: str, invited_by: str, token_hash: str, expires_at: str) -> Dict[str, Any]:
        invitation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        # One pending invitation per (org, email): a duplicate would let an
        # admin silently upgrade a pending invite's role.
        cursor = await self.db.execute(
            """
            INSERT INTO org_invitations (invitation_id, org_id, email, role, invited_by, token_hash, status, created_at, expires_at)
            SELECT ?, ?, ?, ?, ?, ?, 'pending', ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM org_invitations
                WHERE org_id = ? AND email = ? AND status = 'pending'
            )
            """,
            (invitation_id, org_id, email, role, invited_by, token_hash, now, expires_at, org_id, email),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return {}
        return {"invitation_id": invitation_id, "org_id": org_id, "email": email, "role": role,
                "invited_by": invited_by, "expires_at": expires_at}

    async def list_invitations(self, org_id: str, status: str = "pending") -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT invitation_id, org_id, email, role, invited_by, status, created_at, expires_at FROM org_invitations WHERE org_id = ? AND status = ? ORDER BY created_at DESC",
            (org_id, status),
        )
        return [dict(r) for r in rows]

    async def accept_invitation(self, token_hash: str, user_id: str, email: str) -> Dict[str, Any]:
        """Atomically accept a pending invitation (Phase 1.7): the whole
        read-validate-write sequence runs on an OWNED connection inside
        BEGIN IMMEDIATE (DatabaseManager.write_transaction), so unrelated
        asynchronous work can never interleave mid-acceptance and a failure
        rolls back everything. The audit row commits in the same transaction
        (Phase 1.14 — a membership that outlives its audit record is not
        possible here). Raises ValueError with the reason.
        """
        now = datetime.now(timezone.utc)
        async with self.db.write_transaction() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM org_invitations WHERE token_hash = ? AND status = 'pending'",
                    (token_hash,),
                )
            ).fetchone()
            if not row:
                raise ValueError("Invitation not found or already used")
            if row["email"].casefold() != str(email).casefold():
                raise ValueError("This invitation was issued to a different email address")
            # §1.7 robustness: a malformed/legacy expiry (naive datetime or
            # unparseable string) must produce a clean REJECTION, never a
            # 500 — and must mark the invitation expired rather than leave
            # it pending forever.
            try:
                expires = datetime.fromisoformat(row["expires_at"])
                if expires.tzinfo is None:
                    from datetime import timezone as _tz
                    expires = expires.replace(tzinfo=_tz.utc)
                expired = expires < now
            except (ValueError, TypeError):
                expired = True
            if expired:
                await conn.execute(
                    "UPDATE org_invitations SET status = 'expired' WHERE invitation_id = ?",
                    (row["invitation_id"],),
                )
                raise ValueError("This invitation has expired")

            # Already a member? Say so WITHOUT touching the invitation (the
            # pre-transaction version mutated the invite before noticing).
            existing = await (
                await conn.execute(
                    "SELECT 1 FROM org_members WHERE org_id = ? AND user_id = ?",
                    (row["org_id"], user_id),
                )
            ).fetchone()
            if existing:
                raise ValueError("Already a member of this organization")

            cursor = await conn.execute(
                """
                INSERT INTO org_members (org_id, user_id, role, added_at, added_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(org_id, user_id) DO NOTHING
                """,
                (row["org_id"], user_id, row["role"], now.isoformat(), row["invited_by"]),
            )
            if cursor.rowcount == 0:
                raise ValueError("Already a member of this organization")

            # Guarded status flip: if a concurrent revoke/expiry won the race
            # before we got the write lock, the UPDATE matches nothing and the
            # whole acceptance (including the membership insert) rolls back —
            # one consistent outcome for accept-vs-revoke.
            flip = await conn.execute(
                """
                UPDATE org_invitations
                SET status = 'accepted', accepted_at = ?, accepted_by = ?
                WHERE invitation_id = ? AND status = 'pending'
                """,
                (now.isoformat(), user_id, row["invitation_id"]),
            )
            if flip.rowcount == 0:
                raise ValueError("Invitation is no longer pending")

            await conn.execute(
                """
                INSERT INTO audit_logs (
                    event_type, actor_id, actor_type, resource_id, details, timestamp
                ) VALUES (?, ?, 'USER', ?, ?, ?)
                """,
                (
                    "ORG_INVITATION_ACCEPTED", user_id, row["org_id"],
                    json.dumps({"role": row["role"], "atomic_with_acceptance": True}),
                    now.isoformat(),
                ),
            )
            return {"org_id": row["org_id"], "role": row["role"]}

    async def resend_invitation(self, org_id: str, invitation_id: str, invited_by: str,
                                new_token_hash: str, new_expires_at: str) -> Optional[Dict[str, Any]]:
        """Resend policy (Phase 1.5): rotates the token, extends expiry, and
        PRESERVES the originally authorized role — a resend can never become
        a hidden role-escalation route. Only pending invitations can be
        resent; the pending-uniqueness invariant is untouched (same row)."""
        cursor = await self.db.execute(
            """
            UPDATE org_invitations
            SET token_hash = ?, expires_at = ?, invited_by = ?
            WHERE invitation_id = ? AND org_id = ? AND status = 'pending'
            """,
            (new_token_hash, new_expires_at, invited_by, invitation_id, org_id),
        )
        await self.db.commit()
        if cursor.rowcount == 0:
            return None
        row = await self.db.fetch_one(
            "SELECT invitation_id, org_id, email, role, expires_at FROM org_invitations WHERE invitation_id = ?",
            (invitation_id,),
        )
        return dict(row)

    async def get_invitation_by_token(self, token_hash: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetch_one(
            "SELECT invitation_id, org_id, email, role, status, created_at, expires_at FROM org_invitations WHERE token_hash = ?",
            (token_hash,),
        )
        return dict(row) if row else None

    async def revoke_invitation(self, org_id: str, invitation_id: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE org_invitations SET status = 'revoked' WHERE invitation_id = ? AND org_id = ? AND status = 'pending'",
            (invitation_id, org_id),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) > 0

    async def add_member(self, org_id: str, user_id: str, role: str, added_by: str) -> bool:
        if await self.get_org(org_id) is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            """
            INSERT INTO org_members (org_id, user_id, role, added_at, added_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(org_id, user_id) DO NOTHING
            """,
            (org_id, user_id, role, now, added_by),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def set_member_role(self, org_id: str, user_id: str, role: str) -> bool:
        cursor = await self.db.execute(
            """
            UPDATE org_members SET role = ?
            WHERE org_id = ? AND user_id = ? AND role != 'owner'
            """,
            (role, org_id, user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def transfer_ownership(self, org_id: str, current_owner_id: str,
                                 new_owner_id: str) -> Dict[str, Any]:
        """Phase 1.8 dedicated ownership transfer — the ONLY operation that
        moves the owner role. One owned-connection transaction:

        1. Verify the org still exists and the caller IS its current owner
           via a guarded UPDATE (`WHERE owner_id = ?`) — two concurrent
           transfers race and exactly one wins; a stale caller loses.
        2. Verify the target is a CURRENT member (never created on transfer).
        3. Promote the target to owner and demote the outgoing owner to
           admin in the same transaction, so the single-owner invariant
           (exactly one 'owner' membership per org) holds at every instant
           an outside reader can observe.
        4. Append the ORG_OWNERSHIP_TRANSFERRED audit row atomically.

        Raises ValueError with the reason on any violation."""
        now = datetime.now(timezone.utc).isoformat()
        async with self.db.write_transaction() as conn:
            org = await (
                await conn.execute(
                    "SELECT org_id, owner_id FROM orgs WHERE org_id = ?", (org_id,)
                )
            ).fetchone()
            if not org:
                raise ValueError("Organization not found")
            if org["owner_id"] != current_owner_id:
                raise ValueError("Only the current owner can transfer ownership")
            if new_owner_id == current_owner_id:
                raise ValueError("The owner cannot transfer ownership to themselves")

            target = await (
                await conn.execute(
                    "SELECT role FROM org_members WHERE org_id = ? AND user_id = ?",
                    (org_id, new_owner_id),
                )
            ).fetchone()
            if not target:
                raise ValueError("The new owner must be a current member")
            if target["role"] == "owner":
                raise ValueError("Ownership is already held by this member")

            # Guarded owner swap: the WHERE clause is the concurrency check.
            swap = await conn.execute(
                "UPDATE orgs SET owner_id = ? WHERE org_id = ? AND owner_id = ?",
                (new_owner_id, org_id, current_owner_id),
            )
            if swap.rowcount == 0:
                raise ValueError("Ownership changed concurrently; reload and retry")

            promote = await conn.execute(
                """
                INSERT INTO org_members (org_id, user_id, role, added_at, added_by)
                VALUES (?, ?, 'owner', ?, ?)
                ON CONFLICT(org_id, user_id) DO UPDATE SET role = 'owner'
                """,
                (org_id, new_owner_id, now, current_owner_id),
            )
            await conn.execute(
                """
                UPDATE org_members SET role = 'admin'
                WHERE org_id = ? AND user_id = ? AND role = 'owner'
                """,
                (org_id, current_owner_id),
            )

            # Single-owner invariant, verified inside the transaction.
            owners = await (
                await conn.execute(
                    "SELECT COUNT(*) AS n FROM org_members WHERE org_id = ? AND role = 'owner'",
                    (org_id,),
                )
            ).fetchone()
            if int(owners["n"]) != 1:
                raise RuntimeError("ownership transfer violated the single-owner invariant")

            await conn.execute(
                """
                INSERT INTO audit_logs (
                    event_type, actor_id, actor_type, resource_id, details, timestamp
                ) VALUES ('ORG_OWNERSHIP_TRANSFERRED', ?, 'USER', ?, ?, ?)
                """,
                (current_owner_id, org_id,
                 json.dumps({"previous_owner": current_owner_id, "new_owner": new_owner_id,
                             "previous_owner_role": "admin"}),
                 now),
            )
            return {"org_id": org_id, "previous_owner": current_owner_id,
                    "new_owner": new_owner_id, "previous_owner_role": "admin"}

    async def remove_member(self, org_id: str, user_id: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM org_members WHERE org_id = ? AND user_id = ? AND role != 'owner'",
            (org_id, user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_org(self, org_id: str) -> bool:
        conn = await self.db.connection()
        cursor = await conn.execute("DELETE FROM orgs WHERE org_id = ?", (org_id,))
        await conn.commit()
        return cursor.rowcount > 0
