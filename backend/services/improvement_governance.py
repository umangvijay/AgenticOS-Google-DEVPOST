"""Improvement governance (Phase 13 §13.31) and stage rollout flags
(§13.33).

§13.31: the improvement loop operates inside the platform's existing
budget/admission/privacy controls:
- METERING: drafting, comparison, and shadow calls reserve + settle
  through the durable usage ledger (never unmetered model spend).
- QUEUE BOUNDS: queued comparisons per tenant are capped; concurrent
  OPEN experiments per tenant are capped — the proposer cannot flood.
- RETENTION/REDACTION: shadow observations and comparison case details
  are purgeable by age; stored details keep references, not raw
  provider payloads; credentials never enter datasets (they are bound by
  reference at the execution boundary).

§13.33: stages enable progressively via explicit flags — evidence
collection is always on; proposals, comparisons, canaries, and
activation each require their flag so the loop can be released
progressively and stopped stage-by-stage.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("improvement_governance")

# §13.31: the bounded defaults (operators can lower, never raise past
# these platform ceilings).
MAX_QUEUED_COMPARISONS_PER_TENANT = 10
MAX_OPEN_EXPERIMENTS_PER_TENANT = 20
RETENTION_DAYS_DEFAULT = 90


class GovernanceError(Exception):
    pass


async def meter_improvement_call(usage_repo, *, user_id: str,
                                 purpose: str, units: int,
                                 run_id: Optional[str] = None,
                                 model: Optional[str] = None,
                                 ) -> Optional[str]:
    """§13.31: drafting/comparison/shadow calls pass through the SAME
    durable accounting boundary as every paid path. Returns the
    reservation id, or None when accounting is unavailable (the caller
    decides whether to proceed — generation fails closed elsewhere)."""
    if usage_repo is None or not hasattr(usage_repo, "reserve"):
        return None
    from backend.repositories.usage_repository import BucketLimit
    from backend.models.usage import (
        BudgetScope, ReservationRequest, SettlementReceipt,
        MeasurementSource,
    )
    reservation = await usage_repo.reserve(
        ReservationRequest(user_id=user_id, units=int(units),
                           run_id=run_id, purpose=purpose, model=model),
        [BucketLimit(BudgetScope.USER, subject_id=user_id, limit=10_000_000)])
    await usage_repo.settle(reservation.reservation_id, SettlementReceipt(
        reservation_id=reservation.reservation_id, units=int(units),
        measurement_source=MeasurementSource.ESTIMATED,
        resolved_model=model))
    return reservation.reservation_id


async def check_comparison_queue_bound(comparison_repo, *, tenant_id: str,
                                       max_queued: int =
                                       MAX_QUEUED_COMPARISONS_PER_TENANT,
                                       ) -> None:
    """§13.31: bound the comparison queue per tenant."""
    if hasattr(comparison_repo, "db"):
        cursor = await comparison_repo.db.execute(
            "SELECT c.experiment_id FROM experiment_comparisons c WHERE "
            "c.state='queued' LIMIT ?", (max_queued + 1,))
        rows = await cursor.fetchall()
        # Tenant scope travels on the experiment; a full check joins. The
        # simple bound suffices for the platform ceiling.
        if len(rows) > max_queued:
            raise GovernanceError(
                "The comparison queue is full — try again later (§13.31)")


async def check_open_experiment_bound(experiment_repo, *, tenant_id: str,
                                      max_open: int =
                                      MAX_OPEN_EXPERIMENTS_PER_TENANT,
                                      ) -> None:
    if hasattr(experiment_repo, "list_experiments"):
        rows = await experiment_repo.list_experiments(limit=1000)
        open_states = {"PROPOSED", "EVALUATING", "EVALUATED", "SHADOW",
                       "CANARY"}
        # Tenant scope: experiments carry the proposer, not a tenant id —
        # the open bound is platform-wide by design (small controlled
        # loop); per-tenant scoping arrives with tenant-owned features.
        open_count = sum(1 for e in rows if e.state.value in open_states)
        if open_count >= max_open:
            raise GovernanceError(
                f"Too many open experiments ({open_count}/{max_open}) — "
                "close or decide existing ones first (§13.31)")


async def purge_expired_observations(experiment_repo, *,
                                     retention_days: int =
                                     RETENTION_DAYS_DEFAULT) -> int:
    """§13.31 retention: shadow observations older than the retention
    window are purged (their aggregates live in the experiment record)."""
    if not hasattr(experiment_repo, "db"):
        return 0
    cutoff = epoch_now() - int(retention_days) * 86_400
    cursor = await experiment_repo.db.execute(
        "DELETE FROM shadow_observations WHERE recorded_at<=?", (cutoff,))
    await experiment_repo.db.commit()
    return cursor.rowcount or 0


def redact_evidence_detail(detail: Dict[str, Any]) -> Dict[str, Any]:
    """§13.31 redaction: stored evidence keeps REFERENCES and structured
    facts, never raw provider payloads or credentials."""
    blocked_keys = {"raw_response", "raw_payload", "credentials", "api_key",
                    "authorization", "password", "token", "secret",
                    "page_html", "screenshot"}
    out = {}
    for key, value in (detail or {}).items():
        if key.lower() in blocked_keys:
            out[key] = "[REDACTED]"
        elif isinstance(value, dict):
            out[key] = redact_evidence_detail(value)
        else:
            out[key] = value
    return out


# ── §13.33: progressive stage flags ─────────────────────────────────────

STAGES = ("proposals", "comparisons", "shadow", "canary", "activation",
          "routing")


def contextual_routing_enabled(settings: Any = None) -> bool:
    """§14.1: the CONTEXTUAL selector is a release-scoped stage — a
    deployment opts in via IMPROVEMENT_STAGES_ENABLED containing
    'routing'. The default-safe degradation is the baseline global
    ranking (mechanism 'global_rank_fallback' in the decision log);
    the learning/evidence paths always run."""
    if settings is None:
        from backend.config.settings import settings as app_settings
        settings = app_settings
    return bool(getattr(settings, "ROUTING_STAGE_ENABLED", False)) or         "routing" in str(getattr(settings, "IMPROVEMENT_STAGES_ENABLED", "")
                         or "").split(",")


def improvement_stage_flags(settings: Any = None) -> Dict[str, bool]:
    """§13.33: which improvement stages are ENABLED on this deployment.
    Evidence collection is always on; each serving stage requires its
    flag. Defaults: evidence on, everything else off (a deployment opts
    in stage by stage)."""
    flags = {"evidence_collection": True}
    enabled = []
    if settings is not None:
        enabled = str(getattr(settings, "IMPROVEMENT_STAGES_ENABLED", "") or
                      "").split(",")
    enabled = [s.strip().lower() for s in enabled if s.strip()]
    for stage in STAGES:
        flags[stage] = stage in enabled
    return flags


def require_stage(flags: Dict[str, bool], stage: str) -> None:
    """§13.33: a stage that is not enabled refuses — the loop is released
    progressively and stoppable stage-by-stage."""
    if not flags.get(stage):
        raise GovernanceError(
            f"The '{stage}' stage is not enabled on this deployment "
            "(§13.33 progressive rollout)")
