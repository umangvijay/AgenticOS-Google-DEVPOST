"""Firestore effect-ledger repository (Phase 5 §5.4/§5.11 — parity).

The SAME contract as the SQLite SQLiteEffectRepository against Firestore:

- Deterministic document identities: effects/{effect_id},
  effect_receipts/{receipt_id}. The §5.2 digest-dedup is enforced INSIDE
  an async transaction (read-by-digest, then create) — a transport retry
  with a new effect id but the same semantic request returns the
  EXISTING effect (created=False), mirroring the SQLite unique digest
  index (excluding CANCELLED_BEFORE_DISPATCH).
- §5.11 conditional transitions: expected version CAS inside the
  transaction; two resolvers racing an uncertain outcome produce one
  winner and one conflict.
- §5.9 receipts are content-addressed by response digest where present
  (INSERT OR IGNORE parity: the same evidence is not double-recorded).
- Nothing inside a transaction callback publishes or calls providers.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now
from backend.models.effect import (
    Effect,
    EffectReceipt,
    EffectState,
    EffectTransitionError,
    effect_transition_valid,
)

logger = logging.getLogger(__name__)

EFFECTS = "effects"
RECEIPTS = "effect_receipts"
DIGEST_INDEX = "effect_digest_index"

# CANCELLED_BEFORE_DISPATCH frees the digest (SQLite partial unique index
# parity): a deliberate re-send after cancellation gets a NEW effect.
_DIGEST_FREE_STATE = "CANCELLED_BEFORE_DISPATCH"


def _digest_anchor_id(owner_user_id: str, operation_name: str,
                      request_digest: str) -> str:
    return hashlib.sha256(
        f"{owner_user_id}|{operation_name}|{request_digest}".encode()
    ).hexdigest()


def _doc_to_effect(data: Dict[str, Any], effect_id: str) -> Effect:
    return Effect(
        effect_id=effect_id, operation_name=data.get("operation_name", ""),
        owner_user_id=data.get("owner_user_id", ""),
        organization_id=data.get("organization_id"),
        command_id=data.get("command_id"),
        run_id=data.get("run_id"), task_id=data.get("task_id"),
        operation_instance_id=data.get("operation_instance_id"),
        connector_id=data.get("connector_id"),
        connector_revision=data.get("connector_revision"),
        external_account_id=data.get("external_account_id"),
        credential_reference=data.get("credential_reference"),
        target=data.get("target") or "",
        canonical_arguments=dict(data.get("canonical_arguments") or {}),
        request_digest=data.get("request_digest", ""),
        approval_reference=data.get("approval_reference"),
        state=EffectState(data.get("state", "PREPARED")),
        state_version=int(data.get("state_version") or 1),
        provider_idempotency_key=data.get("provider_idempotency_key"),
        compensates_effect_id=data.get("compensates_effect_id"),
        created_at=int(data.get("created_at") or 0),
        updated_at=int(data.get("updated_at") or 0),
    )


def _effect_to_doc(effect: Effect) -> Dict[str, Any]:
    return {
        "operation_name": effect.operation_name,
        "owner_user_id": effect.owner_user_id,
        "organization_id": effect.organization_id,
        "command_id": effect.command_id,
        "run_id": effect.run_id,
        "task_id": effect.task_id,
        "operation_instance_id": effect.operation_instance_id,
        "connector_id": effect.connector_id,
        "connector_revision": effect.connector_revision,
        "external_account_id": effect.external_account_id,
        "credential_reference": effect.credential_reference,
        "target": effect.target,
        "canonical_arguments": dict(effect.canonical_arguments),
        "request_digest": effect.request_digest,
        "approval_reference": effect.approval_reference,
        "state": effect.state.value,
        "state_version": effect.state_version,
        "provider_idempotency_key": effect.provider_idempotency_key,
        "compensates_effect_id": effect.compensates_effect_id,
        "created_at": effect.created_at,
        "updated_at": effect.updated_at,
    }


class FirestoreEffectRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def prepare_effect(self, effect: Effect) -> Tuple[Effect, bool]:
        """§5.5 step 3-4 on Firestore: prepare-or-retrieve.

        A query cannot lock an ABSENCE, so the digest uniqueness is carried
        by a deterministic ANCHOR document
        (effect_digest_index/{sha256(owner|operation|digest)}) created with
        transaction.create() — concurrent duplicates contend on that one
        document: exactly one create() wins, the loser's transaction aborts
        and its retry finds the anchor. A transport retry with a new effect
        id but the same semantic request therefore returns the EXISTING
        effect (created=False), mirroring the SQLite unique digest index."""
        db = await self._get_db()
        now = epoch_now()
        effect.created_at = now
        effect.updated_at = now
        doc = _effect_to_doc(effect)
        effects = db.collection(EFFECTS)
        anchor_ref = db.collection(DIGEST_INDEX).document(_digest_anchor_id(
            effect.owner_user_id, effect.operation_name,
            effect.request_digest))
        anchor_doc = {
            "effect_id": effect.effect_id,
            "owner_user_id": effect.owner_user_id,
            "operation_name": effect.operation_name,
            "request_digest": effect.request_digest,
        }

        @fs_types.async_transactional
        async def prepare(transaction):
            anchor = await anchor_ref.get(transaction=transaction)
            if anchor.exists:
                existing_id = (anchor.to_dict() or {}).get("effect_id")
                if existing_id:
                    fx_snap = await effects.document(existing_id).get(
                        transaction=transaction)
                    if fx_snap.exists:
                        data = fx_snap.to_dict() or {}
                        if data.get("state") != _DIGEST_FREE_STATE:
                            return _doc_to_effect(data, existing_id), False
                # Digest freed by cancellation — rebind the anchor to the
                # new effect (SQLite inserts a second, non-cancelled row).
                transaction.set(anchor_ref, anchor_doc)
                transaction.create(effects.document(effect.effect_id), doc)
                return effect, True
            transaction.create(anchor_ref, anchor_doc)
            transaction.create(effects.document(effect.effect_id), doc)
            return effect, True

        from backend.repositories.firestore.txn_retry import run_transaction
        return await run_transaction(db, prepare)

    async def find_by_digest(self, owner_user_id: str, operation_name: str,
                             request_digest: str) -> Optional[Effect]:
        db = await self._get_db()
        query = (db.collection(EFFECTS).where("owner_user_id", "==", owner_user_id)
                 .where("operation_name", "==", operation_name)
                 .where("request_digest", "==", request_digest).limit(5))
        best: Optional[Effect] = None
        async for snap in query.stream():
            data = snap.to_dict() or {}
            if data.get("state") == _DIGEST_FREE_STATE:
                continue
            fx = _doc_to_effect(data, snap.id)
            if best is None or fx.created_at > best.created_at:
                best = fx
        return best

    async def get_effect(self, effect_id: str) -> Optional[Effect]:
        snap = await (await self._get_db()).collection(EFFECTS).document(
            effect_id).get()
        if not snap.exists:
            return None
        return _doc_to_effect(snap.to_dict() or {}, effect_id)

    async def transition(self, effect_id: str, target: EffectState, *,
                         expected_version: Optional[int] = None,
                         actor: str = "system",
                         reason: str = "") -> Effect:
        """§5.11 on Firestore: conditional transition — expected version
        inside the transaction; stale resolvers get a conflict."""
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(EFFECTS).document(effect_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise EffectTransitionError("Effect not found",
                                            EffectState.PREPARED, 0)
            current = _doc_to_effect(snap.to_dict() or {}, effect_id)
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
            transaction.update(ref, {
                "state": target.value,
                "state_version": current.state_version + 1,
                "updated_at": now,
            })

        await apply(db.transaction())
        return await self.get_effect(effect_id)

    async def record_receipt(self, receipt: EffectReceipt) -> None:
        """§5.9 on Firestore: the same evidence (same response digest for
        the same attempt) is not double-recorded — INSERT OR IGNORE
        parity via a deterministic document id."""
        digest_part = receipt.response_digest or uuid.uuid4().hex
        receipt_id = "rcp_" + hashlib.sha256(
            f"{receipt.effect_id}|{receipt.effect_attempt_id}|{digest_part}"
            .encode()).hexdigest()[:20]
        doc = {
            "effect_id": receipt.effect_id,
            "effect_attempt_id": receipt.effect_attempt_id,
            "provider": receipt.provider,
            "external_account_scope": receipt.external_account_scope,
            "provider_request_id": receipt.provider_request_id,
            "provider_operation_id": receipt.provider_operation_id,
            "remote_resource_id": receipt.remote_resource_id,
            "provider_status": receipt.provider_status,
            "accepted_at": receipt.accepted_at,
            "observed_at": receipt.observed_at or epoch_now(),
            "response_digest": receipt.response_digest,
            "evidence_reference": receipt.evidence_reference,
            "receipt_schema_version": receipt.receipt_schema_version,
            "detail": dict(receipt.detail or {}),
        }
        db = await self._get_db()
        # create() — a concurrent duplicate surfaces as an error and is
        # swallowed (INSERT OR IGNORE parity).
        try:
            await db.collection(RECEIPTS).document(receipt_id).create(doc)
        except Exception:
            logger.debug("Receipt %s already recorded", receipt_id)

    async def receipts_for(self, effect_id: str) -> list:
        db = await self._get_db()
        query = (db.collection(RECEIPTS).where("effect_id", "==", effect_id)
                 .limit(50))
        rows = []
        async for snap in query.stream():
            d = dict(snap.to_dict() or {})
            d["receipt_id"] = snap.id
            rows.append(d)
        rows.sort(key=lambda r: r.get("observed_at") or 0)
        return rows

    async def unresolved_effects(self, limit: int = 50) -> list:
        """§5.10/§5.18 on Firestore: the effect observer's input."""
        db = await self._get_db()
        out = []
        for state in ("DISPATCHING", "PROVIDER_PENDING", "OUTCOME_UNKNOWN"):
            query = (db.collection(EFFECTS).where("state", "==", state)
                     .limit(limit))
            async for snap in query.stream():
                out.append(_doc_to_effect(snap.to_dict() or {}, snap.id))
        out.sort(key=lambda e: e.updated_at)
        return out[:limit]

    async def effects_for_run(self, run_id: str) -> list:
        """Phase 12: all effects bound to a run, for evidence-based
        evaluation."""
        db = await self._get_db()
        query = (db.collection(EFFECTS).where("run_id", "==", run_id)
                 .limit(200))
        out = []
        async for snap in query.stream():
            out.append(_doc_to_effect(snap.to_dict() or {}, snap.id))
        out.sort(key=lambda e: e.created_at)
        return out
