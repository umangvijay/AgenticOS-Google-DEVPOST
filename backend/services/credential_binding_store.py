"""Durable credential-binding lifecycle (§29.5).

The `CredentialBinding` model existed but was never persisted: storing a
secret replaced the ciphertext with no version or epoch recorded, so
rotating an LLM BYOK key left stale validation evidence "current" and
revocation had no path at all. This store gives every named credential
a durable binding bound to its owner, reference, version, epoch and
environment:

- create on first store; RE-STORE IS A ROTATION (secret_version and
  authorization_epoch both advance → the binding id changes → evidence
  computed by `credential_binding_id_from` under the OLD binding stops
  being current);
- revoke on delete (the binding survives the secret as REVOKED —
  "deleted" evidence is distinguishable from "never existed");
- `binding_id_from_owner` reproduces the tool-validation binding id, so
  connector evidence and this store agree by construction.

Raw secret values never enter these rows (§6.3).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credential_bindings (
    owner_id TEXT NOT NULL,
    secret_reference TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    secret_version TEXT NOT NULL,
    authorization_epoch TEXT NOT NULL,
    provider_account_id TEXT,
    granted_scopes_json TEXT NOT NULL DEFAULT '[]',
    environment TEXT NOT NULL DEFAULT 'production',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, secret_reference)
);
"""


class CredentialBindingStore:
    """SQLite reference implementation."""

    def __init__(self, db):
        self.db = db
        self._ready = False

    async def _ensure_schema(self) -> None:
        if self._ready:
            return
        await self.db.execute(_SCHEMA)
        await self.db.commit()
        self._ready = True

    # ── identity helpers ───────────────────────────────────────────────

    @staticmethod
    def binding_id_for(secret_reference: str, secret_version: str,
                       authorization_epoch: str) -> str:
        """The SAME digest the tool-validation context uses
        (`credential_binding_id_from`: sha256 of ref|version|epoch,
        truncated to 32 hex) — evidence and the binding store agree by
        construction. The digest identifies the CONTEXT, not the owner;
        ownership is enforced by the store's (owner, reference) primary
        key and by the per-owner secrets repository."""
        return hashlib.sha256(
            f"{secret_reference}|{secret_version}|{authorization_epoch}"
            .encode()).hexdigest()[:32]

    # ── lifecycle ──────────────────────────────────────────────────────

    async def record_store(self, *, owner_id: str, name: str,
                           environment: str = "production",
                           provider_account_id: Optional[str] = None,
                           granted_scopes: Optional[List[str]] = None,
                           ) -> Dict[str, object]:
        """Create-or-ROTATE the binding for one named credential. A new
        name starts at v1/e1; re-storing advances BOTH (a rotation
        invalidates evidence bound to the previous context)."""
        await self._ensure_schema()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        secret_reference = f"cred:{name}"
        async with self.db.write_transaction() as conn:
            row = await conn.execute(
                "SELECT secret_version, authorization_epoch FROM "
                "credential_bindings WHERE owner_id=? AND "
                "secret_reference=? AND status='active'",
                (owner_id, secret_reference))
            existing = await row.fetchone()
            if existing:
                # ROTATION: bump version + epoch → new binding id.
                try:
                    version_n = int(str(existing["secret_version"]).lstrip("v"))
                except ValueError:
                    version_n = 0
                try:
                    epoch_n = int(str(existing["authorization_epoch"]).lstrip("e"))
                except ValueError:
                    epoch_n = 0
                secret_version = f"v{version_n + 1}"
                epoch = f"e{epoch_n + 1}"
            else:
                secret_version, epoch = "v1", "e1"
            binding_id = self.binding_id_for(secret_reference,
                                             secret_version, epoch)
            import json as _json
            await conn.execute(
                """INSERT INTO credential_bindings
                   (owner_id, secret_reference, binding_id, secret_version,
                    authorization_epoch, provider_account_id,
                    granted_scopes_json, environment, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                   ON CONFLICT(owner_id, secret_reference) DO UPDATE SET
                     binding_id=excluded.binding_id,
                     secret_version=excluded.secret_version,
                     authorization_epoch=excluded.authorization_epoch,
                     provider_account_id=excluded.provider_account_id,
                     granted_scopes_json=excluded.granted_scopes_json,
                     environment=excluded.environment,
                     status='active',
                     updated_at=excluded.updated_at""",
                (owner_id, secret_reference, binding_id, secret_version,
                 epoch, provider_account_id,
                 _json.dumps(sorted(granted_scopes or [])),
                 environment, now, now))
        return {"binding_id": binding_id,
                "secret_reference": secret_reference,
                "secret_version": secret_version,
                "authorization_epoch": epoch,
                "rotated": bool(existing),
                "status": "active"}

    async def get_binding(self, owner_id: str, name: str) -> \
            Optional[Dict[str, object]]:
        await self._ensure_schema()
        cursor = await self.db.execute(
            "SELECT * FROM credential_bindings WHERE owner_id=? AND "
            "secret_reference=?",
            (owner_id, f"cred:{name}"))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        import json as _json
        d["granted_scopes"] = _json.loads(d.pop("granted_scopes_json") or "[]")
        return d

    async def revoke(self, owner_id: str, name: str) -> bool:
        """Mark the binding REVOKED — evidence under this context stops
        being current even if the secret bytes linger somewhere."""
        await self._ensure_schema()
        from datetime import datetime, timezone
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE credential_bindings SET status='revoked', "
                "updated_at=? WHERE owner_id=? AND secret_reference=? "
                "AND status='active'",
                (datetime.now(timezone.utc).isoformat(), owner_id,
                 f"cred:{name}"))
            return bool(cursor.rowcount)

    async def is_current(self, owner_id: str, name: str,
                         binding_id: str) -> bool:
        """Is the given binding id the CURRENT active binding?"""
        binding = await self.get_binding(owner_id, name)
        return bool(
            binding and binding.get("status") == "active"
            and binding.get("binding_id") == binding_id)
