"""
AgentOS — provider rate-limit observations (Phase 30 / 30.32)

The PROVIDER rate limit is a distinct quantity from the product's own
budget: source, scope, observed limit, remaining quantity and reset time
come only from actual provider headers/receipts. When the provider omits
them, the display state is explicitly UNAVAILABLE — never a guessed
reset or a universal model limit.

Adapters call ``record_observation`` after a real response with the
fields the provider actually sent; the latest observation per user is
surfaced on the budget snapshot.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_latest: Dict[str, Dict[str, Any]] = {}

_ALLOWED_SOURCES = {"provider_header", "provider_receipt", "provider_error"}


def record_observation(user_id: str, *, provider: str, source: str,
                       scope: Optional[str] = None,
                       limit: Optional[int] = None,
                       remaining: Optional[int] = None,
                       reset_at: Optional[str] = None,
                       window: Optional[str] = None) -> Dict[str, Any]:
    """Persist what the provider ACTUALLY reported for this user.

    Fields the provider omitted stay absent — this module never invents
    a reset time or a default limit."""
    if source not in _ALLOWED_SOURCES:
        raise ValueError(f"unknown rate-limit source {source!r}")

    def _int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    payload: Dict[str, Any] = {
        "provider": provider,
        "source": source,
        "scope": scope,
        "limit": _int(limit),
        "remaining": _int(remaining),
        "reset_at": reset_at,
        "window": window,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _latest[user_id] = payload
    return dict(payload)


def latest_observation(user_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        payload = _latest.get(user_id)
    return dict(payload) if payload else None


def clear(user_id: Optional[str] = None) -> None:
    with _lock:
        if user_id is None:
            _latest.clear()
        else:
            _latest.pop(user_id, None)
