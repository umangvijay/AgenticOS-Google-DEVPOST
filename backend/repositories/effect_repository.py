"""Effect ledger repository (Phase 5 §5.4/§5.11).

One source of truth for intended external actions, extending the existing
idempotency mechanism (§5.4: never two independent ledgers). Resolutions are
CONDITIONAL (expected state + version) inside an owned transaction — two
resolvers racing an uncertain outcome produce exactly one winner and one
conflict (§5.11).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from backend.models.admission import epoch_now
from backend.models.effect import (
    Effect,
    EffectReceipt,
    EffectState,
    EffectTransitionError,
    effect_transition_valid,
)

logger = logging.getLogger(__name__)


def canonicalize_arguments(arguments: Dict[str, Any]) -> str:
    """§5.3 — versioned, operation-aware canonicalization: object keys sort
    deterministically, array order is preserved, omitted-vs-null stays
    distinct, and NO volatile trace ids or secrets belong here (callers bind
    credentials by reference)."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"),
                      default=str)


def request_digest(owner_user_id: str, operation_name: str,
                   arguments: Dict[str, Any]) -> str:
    """§5.3 acceptance property: changing a recipient, target, attachment, or
    amount changes the digest; an identical semantic request does not."""
    canonical = canonicalize_arguments(arguments)
    raw = f"v1|{owner_user_id}|{operation_name}|{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _row_to_effect(row) -> Effect:
    args = row["canonical_arguments_json"]
    try:
        args = json.loads(args or "{}")
    except ValueError:
        args = {}
    return Effect(
        effect_id=row["effect_id"], operation_name=row["operation_name"],
        owner_user_id=row["owner_user_id"],
        organization_id=row["organization_id"], command_id=row["command_id"],
        run_id=row["run_id"], task_id=row["task_id"],
        operation_instance_id=row["operation_instance_id"],
        connector_id=row["connector_id"],
        connector_revision=row["connector_revision"],
        external_account_id=row["external_account_id"],
        credential_reference=row["credential_reference"],
        target=row["target"] or "",
        canonical_arguments=args if isinstance(args, dict) else {},
        request_digest=row["request_digest"],
        approval_reference=row["approval_reference"],
        state=EffectState(row["state"]),
        state_version=int(row["state_version"]),
        provider_idempotency_key=row["provider_idempotency_key"],
        compensates_effect_id=row["compensates_effect_id"],
        created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
    )


class SQLiteEffectRepository:
    def __init__(self, db):
        self.db = db

    async def prepare_effect(self, effect: Effect) -> tuple[Effect, bool]:
        """§5.5 step 3-4: prepare-or-retrieve. The digest unique index makes
        the same semantic request return the SAME effect (a transport retry
        preserves identity); a deliberate second send gets a NEW effect id
        but ALSO a new digest-scoped row only when policy allows — same
        digest within the unsettled window returns the existing effect."""
        now = epoch_now()
        effect.created_at = now
        effect.updated_at = now
        row = effect.__dict__.copy()
        row["canonical_arguments_json"] = json.dumps(effect.canonical_arguments)
        try:
            await self.db.execute(
                """
                INSERT INTO effects
                    (effect_id, operation_name, owner_user_id, organization_id,
                     command_id, run_id, task_id, operation_instance_id,
                     connector_id, connector_revision, external_account_id,
                     credential_reference, target, canonical_arguments_json,
                     request_digest, approval_reference, state, state_version,
                     provider_idempotency_key, compensates_effect_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'PREPARED', 1, ?, ?, ?, ?)
                """,
                (effect.effect_id, effect.operation_name, effect.owner_user_id,
                 effect.organization_id, effect.command_id, effect.run_id,
                 effect.task_id, effect.operation_instance_id,
                 effect.connector_id, effect.connector_revision,
                 effect.external_account_id, effect.credential_reference,
                 effect.target, row["canonical_arguments_json"],
                 effect.request_digest, effect.approval_reference,
                 effect.provider_idempotency_key, effect.compensates_effect_id,
                 now, now))
            await self.db.commit()
            return effect, True
        except Exception:
            await self.db.commit()
            existing = await self.find_by_digest(
                effect.owner_user_id, effect.operation_name,
                effect.request_digest)
            if existing is not None:
                return existing, False
            raise

    async def find_by_digest(self, owner_user_id: str, operation_name: str,
                             request_digest: str) -> Optional[Effect]:
        cursor = await self.db.execute(
            "SELECT * FROM effects WHERE owner_user_id=? AND operation_name=? "
            "AND request_digest=? AND state != 'CANCELLED_BEFORE_DISPATCH' "
            "ORDER BY created_at DESC LIMIT 1",
            (owner_user_id, operation_name, request_digest))
        row = await cursor.fetchone()
        return _row_to_effect(row) if row else None

    async def get_effect(self, effect_id: str) -> Optional[Effect]:
        cursor = await self.db.execute(
            "SELECT * FROM effects WHERE effect_id=?", (effect_id,))
        row = await cursor.fetchone()
        return _row_to_effect(row) if row else None

    async def transition(self, effect_id: str, target: EffectState, *,
                         expected_version: Optional[int] = None,
                         actor: str = "system",
                         reason: str = "") -> Effect:
        """§5.11: conditional transition — expected state+version inside an
        owned transaction; stale resolvers get a conflict."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM effects WHERE effect_id=?", (effect_id,))
            row = await cursor.fetchone()
            if not row:
                raise EffectTransitionError("Effect not found",
                                            EffectState.PREPARED, 0)
            current = _row_to_effect(row)
            if not effect_transition_valid(current.state, target):
                raise EffectTransitionError(
                    f"Invalid effect transition {current.state.value} → "
                    f"{target.value} for {effect_id}", current.state,
                    current.state_version)
            if expected_version is not None and \
                    current.state_version != int(expected_version):
                raise EffectTransitionError(
                    f"Effect version conflict for {effect_id}: durable "
                    f"{current.state_version}, caller {expected_version}",
                    current.state, current.state_version)
            cursor = await conn.execute(
                "UPDATE effects SET state=?, state_version=?, updated_at=? "
                "WHERE effect_id=? AND state_version=?",
                (target.value, current.state_version + 1, now, effect_id,
                 current.state_version))
            if not cursor.rowcount:
                raise EffectTransitionError(
                    f"Concurrent effect change for {effect_id}",
                    current.state, current.state_version)
        return await self.get_effect(effect_id)

    async def record_receipt(self, receipt: EffectReceipt) -> None:
        import uuid as _uuid
        await self.db.execute(
            """
            INSERT OR IGNORE INTO effect_receipts
                (receipt_id, effect_id, effect_attempt_id, provider,
                 external_account_scope, provider_request_id,
                 provider_operation_id, remote_resource_id, provider_status,
                 accepted_at, observed_at, response_digest, evidence_reference,
                 receipt_schema_version, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"rcp_{_uuid.uuid4().hex[:20]}", receipt.effect_id,
             receipt.effect_attempt_id, receipt.provider,
             receipt.external_account_scope, receipt.provider_request_id,
             receipt.provider_operation_id, receipt.remote_resource_id,
             receipt.provider_status, receipt.accepted_at,
             receipt.observed_at or epoch_now(), receipt.response_digest,
             receipt.evidence_reference, receipt.receipt_schema_version,
             json.dumps(receipt.detail)))
        await self.db.commit()

    async def receipts_for(self, effect_id: str) -> list:
        cursor = await self.db.execute(
            "SELECT * FROM effect_receipts WHERE effect_id=? ORDER BY observed_at",
            (effect_id,))
        rows = []
        for r in await cursor.fetchall():
            row = dict(r)
            try:
                row["detail"] = json.loads(row.pop("detail_json") or "{}")
            except (ValueError, KeyError):
                row["detail"] = {}
            rows.append(row)
        return rows

    async def unresolved_effects(self, limit: int = 50) -> list:
        """§5.10/§5.18: the effect observer's input — unknown outcomes whose
        evidence investigation has not closed them."""
        cursor = await self.db.execute(
            "SELECT * FROM effects WHERE state IN ('DISPATCHING',"
            "'PROVIDER_PENDING','OUTCOME_UNKNOWN') ORDER BY updated_at LIMIT ?",
            (limit,))
        return [_row_to_effect(r) for r in await cursor.fetchall()]

    async def effects_for_run(self, run_id: str) -> list:
        """Phase 12: all effects bound to a run, for evidence-based
        evaluation. Uses the idx_effects_run index."""
        cursor = await self.db.execute(
            "SELECT * FROM effects WHERE run_id=? ORDER BY created_at",
            (run_id,))
        return [_row_to_effect(r) for r in await cursor.fetchall()]


_repo: Optional[SQLiteEffectRepository] = None


def set_effect_repo(repo) -> None:
    global _repo
    _repo = repo


def get_effect_repo():
    return _repo
