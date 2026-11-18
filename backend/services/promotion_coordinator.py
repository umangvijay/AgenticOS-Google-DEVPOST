"""Deployment coordinator (Phase 13 §13.19/§13.20).

Promotion changes the EFFECTIVE runtime — not just experiment metadata:

- `promote_and_activate`: after the improvement service's deterministic
  gates pass, the coordinator ACTIVATES the candidate version in the
  prompt store and records an activation receipt with an explicit state
  (pending → effective / failed) — a crash between promotion and
  activation is reconcilable (§13.19 acceptance: the API promotion makes
  subsequent eligible tasks consume the candidate).
- `rollback`: API-triggered rollback supplies the RESTORE automatically
  (the pinned baseline version), refuses missing targets, checks the
  expected current activation so a newer release is never overwritten,
  and distinguishes rollback-requested from rollback-confirmed (§13.20).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.models.admission import epoch_now
from backend.services.prompt_versions import PromptVersionStore

logger = logging.getLogger("promotion_coordinator")


class CoordinatorError(Exception):
    pass


class ActivationReceiptStore:
    """§14.J: the activation receipt is DURABLE, not a return value. It
    is created (state=pending) the moment activation is attempted and
    marked afterwards — a crash in between leaves a reconcilable record
    keyed by experiment identity."""

    def __init__(self, db):
        self.db = db

    async def create_or_get(self, *, experiment_id: str, prompt_id: str,
                            candidate_version_id: str,
                            created_by: str) -> tuple:
        """Create the pending receipt (or return the pre-existing one).
        Returns (receipt, created) — `created` distinguishes a fresh
        attempt from a retry of an interrupted one."""
        import uuid as _uuid
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT * FROM activation_receipts WHERE experiment_id=?",
            (experiment_id,))
        existing = await cursor.fetchone()
        if existing:
            return dict(existing), False
        await self.db.execute(
            """INSERT INTO activation_receipts
               (receipt_id, experiment_id, prompt_id, candidate_version_id,
                state, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (f"acr_{_uuid.uuid4().hex[:20]}", experiment_id, prompt_id,
             candidate_version_id, created_by, now, now))
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT * FROM activation_receipts WHERE experiment_id=?",
            (experiment_id,))
        return dict(await cursor.fetchone()), True

    async def get_for_experiment(self, experiment_id: str)             -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM activation_receipts WHERE experiment_id=?",
            (experiment_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def mark(self, experiment_id: str, state: str, *,
                   active_version_id: Optional[str] = None,
                   error: Optional[str] = None) -> None:
        await self.db.execute(
            "UPDATE activation_receipts SET state=?, active_version_id=?, "
            "error=?, updated_at=? WHERE experiment_id=?",
            (state, active_version_id, error, epoch_now(), experiment_id))
        await self.db.commit()


def _receipt_store(prompt_store) -> Optional[ActivationReceiptStore]:
    db = getattr(prompt_store, "db", None)
    return ActivationReceiptStore(db) if db is not None else None


async def _reconcile_receipt(receipt: Dict[str, Any], prompt_store,
                             experiment_id: str, prompt_id: str,
                             candidate_version_id: str, actor: str, *,
                             store: Optional[ActivationReceiptStore] = None) \
        -> Dict[str, Any]:
    """A PENDING receipt is an interrupted activation — re-drive the
    (idempotent) pointer move and finish the receipt. An EFFECTIVE
    receipt is returned as-is; activation never double-applies. The
    DURABLE state is authoritative: when a receipt store is supplied it
    is re-read before deciding (the passed dict may be stale)."""
    if store is not None:
        durable = await store.get_for_experiment(experiment_id)
        if durable is not None:
            receipt = durable
    if receipt.get("state") == "effective":
        return receipt
    try:
        activated = await prompt_store.activate(
            prompt_id, candidate_version_id, activated_by=f"reconcile:{actor}")
        if store is not None:
            await store.mark(experiment_id, "effective",
                             active_version_id=activated.get("version_id"))
        receipt.update({"state": "effective",
                        "active_version_id": activated.get("version_id")})
    except Exception as exc:
        if store is not None:
            await store.mark(experiment_id, "failed", error=str(exc)[:200])
        receipt.update({"state": "failed", "error": str(exc)[:200]})
        logger.error("Receipt reconciliation failed for %s: %s",
                     experiment_id, exc)
    return receipt


async def promote_and_activate(
    service, prompt_store: PromptVersionStore, experiment_id: str, *,
    approver: str, deployment_version: str,
    total_population: Optional[int] = None,
    expected_active_version_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Promote (gated) then activate the candidate. Returns the promoted
    experiment plus the activation receipt."""
    experiment = await service._require(experiment_id)
    if experiment.kind.value != "prompt":
        raise CoordinatorError(
            f"Activation coordination supports prompt experiments "
            f"(got {experiment.kind.value})")
    prompt_id = experiment.target.split("prompt:", 1)[-1]

    # 1) The gated promotion (deterministic thresholds, permissions).
    promoted = await service.promote(
        experiment_id, approver=approver,
        deployment_version=deployment_version,
        total_population=total_population)

    # 2) §13.21: the expected-current-version check — a newer release is
    # never silently overwritten by a stale coordinator.
    current = await prompt_store.get_active(prompt_id)
    if expected_active_version_id is not None and current is not None and \
            current.get("version_id") != expected_active_version_id:
        raise CoordinatorError(
            f"Active version for {prompt_id} changed since evaluation "
            f"(expected {expected_active_version_id}, found "
            f"{(current or {}).get('version_id')}) — refusing to overwrite "
            "a newer release")

    # 3) Activation with an explicit DURABLE receipt (§14.J): the
    # pending receipt commits BEFORE the activation, so a crash in
    # between is reconcilable by identity — the retry finds the receipt
    # and finishes the pointer move instead of re-running the gates.
    store = _receipt_store(prompt_store)
    if store is not None:
        receipt, created = await store.create_or_get(
            experiment_id=experiment_id, prompt_id=prompt_id,
            candidate_version_id=promoted.candidate_ref,
            created_by=f"promotion:{approver}")
    else:
        receipt, created = {
            "experiment_id": experiment_id, "prompt_id": prompt_id,
            "candidate_version_id": promoted.candidate_ref,
            "state": "pending"}, True
    if receipt.get("state") == "effective":
        # A prior attempt already activated — return the durable receipt.
        return {"experiment": promoted, "activation_receipt": receipt,
                "reconciled": True}
    if not created:
        # A PENDING receipt from an EARLIER interrupted attempt (the
        # crash between promote and activate) is reconciled here: the
        # idempotent pointer move finishes it — the gates are NOT re-run
        # and the candidate is never double-activated.
        receipt = await _reconcile_receipt(
            receipt, prompt_store, experiment_id, prompt_id,
            promoted.candidate_ref, approver, store=store)
        return {"experiment": promoted, "activation_receipt": receipt,
                "reconciled": receipt.get("state") == "effective"}
    try:
        activated = await prompt_store.activate(
            prompt_id, promoted.candidate_ref, activated_by=f"promotion:{approver}")
        if store is not None:
            await store.mark(experiment_id, "effective",
                             active_version_id=activated.get("version_id"))
        receipt["state"] = "effective"
        receipt["active_version_id"] = activated.get("version_id")
        receipt["active_version"] = activated.get("version")
    except Exception as exc:
        if store is not None:
            await store.mark(experiment_id, "failed", error=str(exc)[:200])
        receipt["state"] = "failed"
        receipt["error"] = str(exc)[:200]
        logger.error("Activation failed for experiment %s: %s",
                     experiment_id, exc)
    return {"experiment": promoted, "activation_receipt": receipt}


async def rollback_via_api(
    service, prompt_store: PromptVersionStore, experiment_id: str, *,
    operator: str, reason: str,
) -> Dict[str, Any]:
    """API rollback: the RESTORE is supplied by the coordinator (the
    pinned baseline version) — the caller cannot mark ROLLED_BACK without
    actually reverting the runtime (§13.20 review finding F)."""
    experiment = await service._require(experiment_id)
    if experiment.state.value != "PROMOTED":
        raise CoordinatorError(
            f"Rollback applies to PROMOTED, not {experiment.state.value}")
    if experiment.kind.value == "prompt":
        prompt_id = experiment.target.split("prompt:", 1)[-1]
        current = await prompt_store.get_active(prompt_id)
        if current is None:
            raise CoordinatorError(
                f"No active version for {prompt_id} — nothing to revert")
        if current.get("version_id") == experiment.candidate_ref:
            # The candidate is live — reverting is safe.
            pass
        elif current.get("version_id") == experiment.baseline_ref:
            raise CoordinatorError(
                "The baseline is already active — the candidate was "
                "deactivated elsewhere; refusing a duplicate rollback")
        else:
            # A NEWER release is live: never overwrite it (§13.20).
            raise CoordinatorError(
                f"A different version is active ({current.get('version_id')}) "
                "— rollback would overwrite a newer release")
    from backend.services.improvement_rollout import make_restore
    restore = make_restore(prompt_store=prompt_store)
    rolled = await service.rollback(experiment_id, operator=operator,
                                    reason=reason, restore=restore)
    # Rollback-CONFIRMED: verify the effective runtime actually reverted.
    confirmed = False
    if experiment.kind.value == "prompt":
        prompt_id = experiment.target.split("prompt:", 1)[-1]
        active = await prompt_store.get_active(prompt_id)
        confirmed = bool(active) and active.get("version_id") == experiment.baseline_ref
    return {"experiment": rolled, "rollback_confirmed": confirmed}
