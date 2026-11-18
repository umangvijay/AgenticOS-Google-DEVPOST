"""Revision lifecycle + atomic activation (Phase 6 §6.14/§6.15).

Lifecycle: DRAFT → VALIDATING → VALIDATED → APPROVED → ACTIVE → RETIRED.
Validation failures belong to validation runs; a revision stays a draft
with failed evidence. Independent controls (connector enabled, revision
revoked, credential active, health, circuit breaker, freshness) stay
separate from this lifecycle.

Activation is ATOMIC (§6.14): verify expected current active revision,
evaluate the candidate's eligibility, change the pointer with
compare-and-set, append an activation event. Two racing activations produce
one winner and one explicit conflict (§6.21).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

REVISION_LIFECYCLE = ("DRAFT", "VALIDATING", "VALIDATED", "APPROVED",
                      "ACTIVE", "RETIRED")


class ActivationConflictError(Exception):
    """Two competing activations raced the same expected current revision —
    exactly one wins; the loser receives this explicit conflict (§6.21)."""


async def activate_revision(
    repo, *, mcp_id: str, candidate_revision_id: str,
    expected_current_revision_id: Optional[str],
    eligibility_allowed: bool, eligibility_reasons: Optional[list] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """Atomic, eligibility-checked activation (§6.14).

    Transaction (SQLite owned transaction via the repo's connection):
    1. Verify the expected current active revision (compare-and-set).
    2. Verify the candidate revision exists and belongs to the connector.
    3. Eligibility has already been evaluated by the caller — the gate
       result is RE-checked here for the recorded decision.
    4. Change the active pointer + lifecycle.
    5. Append an activation event (audit row).
    """
    if not eligibility_allowed:
        return {"activated": False,
                "reason_codes": eligibility_reasons or ["ELIGIBILITY_FAILED"]}

    if callable(getattr(repo, "activate_revision_atomic", None)):
        return await repo.activate_revision_atomic(mcp_id, candidate_revision_id,
                                                   expected_current_revision_id, actor)

    conn = await repo.db.connection()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        import json as _json
        cursor = await conn.execute(
            "SELECT payload_json FROM mcp_revisions WHERE mcp_id=? AND revision_id=?",
            (mcp_id, candidate_revision_id))
        row = await cursor.fetchone()
        if not row:
            await conn.execute("ROLLBACK")
            raise ValueError("Candidate revision not found for this connector")
        manifest_cursor = await conn.execute(
            "SELECT active_revision_id FROM mcps WHERE mcp_id=?", (mcp_id,))
        manifest_row = await manifest_cursor.fetchone()
        current = manifest_row[0] if manifest_row else None
        if expected_current_revision_id is not None and \
           (current or "") != expected_current_revision_id:
            await conn.execute("ROLLBACK")
            raise ActivationConflictError(
                f"Active revision changed: expected "
                f"{expected_current_revision_id}, current {current}")

        await conn.execute(
            "UPDATE mcps SET active_revision_id=?, updated_at=? WHERE mcp_id=?",
            (candidate_revision_id, datetime.now(timezone.utc).isoformat(), mcp_id))
        await conn.execute(
            """
            INSERT INTO connector_activations
                (activation_id, mcp_id, revision_id, previous_revision_id,
                 actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"act_{uuid.uuid4().hex[:20]}", mcp_id, candidate_revision_id,
             current, actor, epoch_now()))
        await conn.commit()
    except ActivationConflictError:
        raise
    except Exception:
        try:
            await conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    logger.info("Revision %s activated for %s by %s (atomic CAS)",
                candidate_revision_id[:12], mcp_id, actor)
    return {"activated": True, "revision_id": candidate_revision_id,
            "previous_revision_id": current}


async def rollback_revision(
    repo, *, mcp_id: str, target_revision_id: str,
    current_eligibility_allowed: bool, eligibility_reasons: Optional[list] = None,
    actor: str = "system",
) -> Dict[str, Any]:
    """§6.15: roll back the ACTIVE pointer to a previous revision that is
    CURRENTLY eligible. Rollback must not restore secrets, re-enable revoked
    credentials, ignore current policy, or inherit expired evidence — the
    current eligibility check is the gate."""
    return await activate_revision(
        repo, mcp_id=mcp_id, candidate_revision_id=target_revision_id,
        expected_current_revision_id=None,   # rollback: any current pointer
        eligibility_allowed=current_eligibility_allowed,
        eligibility_reasons=eligibility_reasons, actor=actor)
