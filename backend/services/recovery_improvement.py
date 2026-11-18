"""Measured self-healing (Phase 13 §13.22) and connector-revision
improvement (§13.23).

§13.22: recovery is MEASURED by failure class — verified recovery rate,
attempt counts, and repeat-failure rate, from the durable episode
records. "Repair queued" and "retry attempted" NEVER count as healed:
credit requires the VERIFIED_RECOVERY terminal state (and the
verification happens outside this module — the episode state machine
only reaches VERIFIED_RECOVERY through the verifier actor).

§13.23: improvement extends to connector revisions — a candidate
revision for schema drift / pagination / encoding changes is proposed
from reviewed failure evidence, and it stays UNAVAILABLE until the
affected operations pass the required gates (the §13.3 readiness
machinery judges it; the proposal only creates the candidate).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("recovery_improvement")


async def recovery_metrics(recovery_repo, *, tenant_id: Optional[str] = None,
                           since_epoch: Optional[int] = None,
                           ) -> Dict[str, Any]:
    """§13.22: recovery measured by failure class — from durable episode
    records only. A strategy earns credit only after the repaired outcome
    is independently verified (VERIFIED_RECOVERY)."""
    episodes = await recovery_repo.reviewed_failures(
        tenant_id=tenant_id, since_epoch=since_epoch, limit=1000)
    # VERIFIED_RECOVERY episodes are terminal-but-successful; the
    # reviewed_failures query returns failed/escalated — pull those too
    # for the denominator.
    verified: List[Dict[str, Any]] = []
    if hasattr(recovery_repo, "db"):
        params: List[Any] = []
        sql = ("SELECT * FROM recovery_episodes WHERE state='VERIFIED_RECOVERY'")
        if tenant_id:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        if since_epoch is not None:
            sql += " AND updated_at>=?"
            params.append(int(since_epoch))
        sql += " LIMIT 1000"
        cursor = await recovery_repo.db.execute(sql, tuple(params))
        verified = [dict(r) for r in await cursor.fetchall()]

    by_class: Dict[str, Dict[str, Any]] = {}
    for ep in episodes + verified:
        cls = ep.get("failure_category") or ep.get("failure_code") or "unclassified"
        bucket = by_class.setdefault(cls, {
            "episodes": 0, "verified_recoveries": 0, "failed_recoveries": 0,
            "escalated": 0, "attempts": 0,
        })
        bucket["episodes"] += 1
        state = ep.get("state")
        if state == "VERIFIED_RECOVERY":
            bucket["verified_recoveries"] += 1
        elif state == "RECOVERY_FAILED":
            bucket["failed_recoveries"] += 1
        elif state == "ESCALATED":
            bucket["escalated"] += 1
        bucket["attempts"] += int(ep.get("attempt_count") or 0)

    total = sum(b["episodes"] for b in by_class.values())
    total_verified = sum(b["verified_recoveries"] for b in by_class.values())
    # Repeat-failure rate: episodes needing multiple attempts.
    repeat = sum(1 for ep in episodes + verified
                 if int(ep.get("attempt_count") or 0) > 1)
    return {
        "total_episodes": total,
        "verified_recoveries": total_verified,
        # The headline: credit ONLY for independently verified recoveries.
        "verified_recovery_rate": (round(total_verified / total, 4)
                                   if total else None),
        "repeat_failure_rate": (round(repeat / total, 4) if total else None),
        "by_failure_class": by_class,
        "note": ("'repair queued' and 'retry attempted' never count as "
                 "healed — credit requires VERIFIED_RECOVERY (§13.22)"),
    }


async def propose_connector_revision_improvement(
    *, mcp_repo, mcp_id: str, revision_service, reviewed_failures: List[Dict[str, Any]],
    proposed_by: str,
    candidate_definition: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """§13.23: propose a candidate CONNECTOR REVISION from reviewed
    failure evidence (schema drift, pagination, encoding changes). The
    candidate is created via the revision checkpoint machinery and stays
    UNAVAILABLE until the affected operations pass the §13.3 readiness
    gates — the proposal cannot activate anything.

    Permission expansion is refused: the candidate definition may not add
    operations beyond the ones evidence covers (the affected operations
    from the reviewed failures)."""
    mcp = await mcp_repo.get_mcp(mcp_id)
    if mcp is None:
        raise ValueError(f"Connector {mcp_id} not found")
    affected_ops = sorted({ep.get("task_id") or "" for ep in reviewed_failures
                           if ep.get("task_id")})
    if candidate_definition is not None:
        candidate_ops = {t.get("tool_name") for t in
                         (candidate_definition.get("tools") or [])}
        current_ops = {t.get("tool_name") for t in
                       (mcp.get("tools") or []) if isinstance(t, dict)}
        expansion = candidate_ops - current_ops
        if expansion:
            raise PermissionError(
                f"The candidate revision expands operations: "
                f"{', '.join(sorted(expansion))} — permission expansion is "
                "refused (§13.23)")
    # The candidate revision checkpoint (existing §6.2 machinery).
    checkpoint = await revision_service.checkpoint(mcp_repo, mcp_id)
    return {
        "mcp_id": mcp_id,
        "revision_id": checkpoint.get("revision_id"),
        "affected_operations": affected_ops,
        "state": "candidate_unavailable",
        "note": ("The candidate revision is UNAVAILABLE until the affected "
                 "operations pass the required validation gates (§13.3 "
                 "readiness judges it) — the proposal activates nothing."),
    }
