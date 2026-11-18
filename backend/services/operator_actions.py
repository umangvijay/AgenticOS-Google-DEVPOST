"""
AgentOS — named operator actions (Phase 30 / 30.6)

Operator/inner-loop actions are NAMED, individually permissioned and
produce REDACTED receipts. Support access may inspect incidents, cancel
jobs, reconcile effects, rotate credentials, approve policies and
restore backups — each under its own permission, each emitting only
counts and identifiers, never secret values.

Decrypt, key administration and policy approval are deliberately
SEPARATE permissions (see ``OPERATOR_ACTIONS`` in
``backend/security/permission_matrix.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Values that must never appear in a support-visible receipt. Each entry
# pairs the pattern with its replacement (patterns without capture groups
# replace with a bare marker).
_REDACT_PATTERNS = (
    # Bearer schemes first so "Authorization: Bearer <token>" loses the
    # whole token, not just the scheme name.
    (re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{8,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)(authorization|bearer|api[-_]?key|token|secret|password)"
                r"\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"(?i)gcp://[^\s\"']+"), "gcp://<redacted>"),
)


def redact_text(text: str) -> str:
    """Strip credential-shaped fragments from free text."""
    redacted = text or ""
    for pattern, replacement in _REDACT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


@dataclass
class OperatorReceipt:
    """Redacted receipt for one operator action (support-visible)."""
    action: str
    actor_id: str
    target: str
    outcome: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        safe_details = {
            k: (redact_text(str(v)) if isinstance(v, str) else v)
            for k, v in self.details.items()
        }
        return {
            "action": self.action,
            "actor_id": self.actor_id,
            "target": self.target,
            "outcome": self.outcome,
            "at": self.at,
            "details": safe_details,
            "redacted": True,
        }


async def record_operator_action(audit_repo, *, action: str, actor_id: str,
                                 target: str, outcome: str,
                                 details: Optional[Dict[str, Any]] = None,
                                 allowed: bool = True) -> Optional[OperatorReceipt]:
    """Persist (audit) and return a redacted receipt for an operator action.

    Returns None when ``allowed`` is False — the caller must raise before
    this point; the audit trail still records the denial."""
    receipt = OperatorReceipt(
        action=action, actor_id=actor_id, target=target,
        outcome=outcome, details=dict(details or {}),
    )
    if audit_repo is not None:
        await audit_repo.log_event({
            "event_type": f"OPERATOR_{action.upper().replace('.', '_').replace('-', '_')}"
                          + ("" if allowed else "_DENIED"),
            "actor_id": actor_id, "actor_type": "OPERATOR",
            "resource_id": target,
            "details": receipt.to_dict()["details"],
        })
    return receipt if allowed else None
