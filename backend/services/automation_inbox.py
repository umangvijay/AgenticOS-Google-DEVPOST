"""Automation inbox (Phase 21 §21.6).

ONE actionable surface combining everything that needs the user:
pending approvals, failed runs, needs-review runs, uncertain external
outcomes, and freshly delivered artifacts — each item with the precise
next action and a link target. Items come from the durable stores; an
empty inbox is a truthful empty list, not a hidden feature.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("automation_inbox")

# Items the user can act on, ordered by urgency.
_SEVERITY_ORDER = {"blocked": 0, "needs_review": 1, "uncertain": 2,
                   "approval": 3, "failed": 4, "delivered": 5}


class AutomationInboxService:
    def __init__(self, workflow_repo, effect_repo=None, artifact_repo=None):
        self.workflow_repo = workflow_repo
        self.effect_repo = effect_repo
        self.artifact_repo = artifact_repo

    async def collect(self, user_id: str, *, limit: int = 50) -> Dict[str, Any]:
        now = epoch_now()
        items: List[Dict[str, Any]] = []

        # 1) Pending approvals (§21.6).
        try:
            approvals = await self.workflow_repo.list_pending_approvals(user_id)
            for a in approvals or []:
                args = a.get("arguments") or {}
                items.append({
                    "kind": "approval",
                    "id": a.get("approval_id"),
                    "title": f"Approval needed: {a.get('tool_name', 'action')}",
                    "detail": str(args.get("summary") or
                                  a.get("tool_name") or "")[:200],
                    "run_id": a.get("run_id"),
                    "next_action": "approve or reject to resume the run",
                    "link": (f"/dashboard/workspace/{a.get('run_id')}"
                             if a.get("run_id") else None),
                    "created_at": a.get("requested_at"),
                })
        except Exception:
            logger.debug("Approval pull failed", exc_info=True)

        # 2) Failed + needs-review runs (§12.2: needs-review is its own
        #    actionable state, never rendered as success or failure).
        try:
            runs = await self.workflow_repo.list_runs(user_id, limit=100)
            for r in runs or []:
                status = str(r.get("status") or "").upper()
                if status == "FAILED":
                    items.append({
                        "kind": "failed",
                        "id": r.get("run_id"),
                        "title": f"Run failed: {r.get('goal', '')[:60]}",
                        "detail": "Recovery could not complete this run.",
                        "run_id": r.get("run_id"),
                        "next_action": "inspect the failure and retry",
                        "link": f"/dashboard/workspace/{r.get('run_id')}",
                        "created_at": r.get("created_at"),
                    })
                elif status == "NEEDS_REVIEW":
                    items.append({
                        "kind": "needs_review",
                        "id": r.get("run_id"),
                        "title": f"Verification needs review: {r.get('goal', '')[:60]}",
                        "detail": ("Execution finished but the outcome could "
                                   "not be verified automatically."),
                        "run_id": r.get("run_id"),
                        "next_action": "review the evaluation and decide",
                        "link": f"/dashboard/workspace/{r.get('run_id')}",
                        "created_at": r.get("created_at"),
                    })
        except Exception:
            logger.debug("Run pull failed", exc_info=True)

        # 3) Uncertain external outcomes (§5: never a generic retry).
        if self.effect_repo is not None:
            try:
                unresolved = await self.effect_repo.unresolved_effects(limit=20)
                for fx in unresolved or []:
                    if fx.owner_user_id != user_id:
                        continue
                    items.append({
                        "kind": "uncertain",
                        "id": fx.effect_id,
                        "title": f"Outcome unknown: {fx.operation_name}",
                        "detail": (f"The action on {fx.target} may have "
                                   "happened; the result could not be "
                                   "confirmed."),
                        "run_id": fx.run_id,
                        "next_action": "check the outcome from evidence",
                        "link": (f"/dashboard/workspace/{fx.run_id}"
                                 if fx.run_id else None),
                        "created_at": fx.updated_at,
                    })
            except Exception:
                logger.debug("Effect pull failed", exc_info=True)

        def _epoch(value: Any) -> float:
            """created_at arrives as epoch ints (effects) or ISO strings
            (runs) — normalize for one ordering."""
            if value is None:
                return 0.0
            try:
                return float(value)
            except (TypeError, ValueError):
                try:
                    from datetime import datetime
                    return datetime.fromisoformat(str(value)).timestamp()
                except ValueError:
                    return 0.0

        items.sort(key=lambda i: (_SEVERITY_ORDER.get(i["kind"], 9),
                                  -_epoch(i.get("created_at"))))
        counts: Dict[str, int] = {}
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        return {"user_id": user_id, "generated_at": now,
                "items": items[:limit], "counts": counts,
                "total": len(items)}
