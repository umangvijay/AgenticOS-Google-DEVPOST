"""In-process Playwright sessions kept alive while a human completes CAPTCHA/OTP/MFA."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Bounded session leases (v5.6 A7): a paused session held for a human
# challenge must not live in the worker's memory forever. Sessions older
# than the lease are swept and closed.
SESSION_LEASE_SECONDS = 1800


@dataclass
class BrowserSession:
    playwright: Any
    browser: Any
    context: Any
    page: Any
    allowed_domains: Set[str]
    secrets: Dict[str, str]
    goal: str
    history: List[str]
    steps: List[Dict[str, Any]]
    step_num: int
    max_steps: int
    challenge_type: str
    url: str
    headed: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=lambda: time.time())


_SESSIONS: Dict[str, BrowserSession] = {}


def session_key(run_id: str, task_id: str) -> str:
    return f"{run_id}:{task_id}"


def get_session(run_id: str, task_id: str) -> Optional[BrowserSession]:
    return _SESSIONS.get(session_key(run_id, task_id))


def put_session(run_id: str, task_id: str, session: BrowserSession) -> None:
    _SESSIONS[session_key(run_id, task_id)] = session


async def _close_resources(session: BrowserSession) -> bool:
    complete = True
    for closer in (
        (session.context.close if session.context else None),
        (session.browser.close if session.browser else None),
        (session.playwright.stop if session.playwright else None),
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            complete = False
            logger.warning("Browser resource cleanup failed", exc_info=True)
    return complete


async def close_session(run_id: str, task_id: str) -> bool:
    key = session_key(run_id, task_id)
    session = _SESSIONS.get(key)
    if not session:
        return False
    return await _close_registered(key, session)


async def _close_registered(key: str, session: BrowserSession) -> bool:
    complete = await _close_resources(session)
    session_id = session.extra.get("durable_session_id")
    if session_id:
        from backend.repositories.browser_session_repository import get_browser_session_repo
        from backend.services.capacity_service import WORKER_ID
        repo = get_browser_session_repo()
        if repo is None:
            return False  # retain handles and retry the durable acknowledgement
        # Compare-and-set in the store, not a read followed by an unfenced write.
        await repo.acknowledge_cleanup(session_id, WORKER_ID,
                                       session.extra.get("owner_epoch"), complete=complete)
    if complete and _SESSIONS.get(key) is session:
        _SESSIONS.pop(key, None)
    return complete


async def sweep_expired_sessions(lease_seconds: int = SESSION_LEASE_SECONDS) -> int:
    """Close and drop sessions whose lease expired — bounded memory on the
    worker even when many challenge pauses accumulate."""
    now = time.time()
    expired = [key for key, s in _SESSIONS.items()
               if now - getattr(s, "created_at", 0) > lease_seconds]
    closed = 0
    for key in expired:
        session = _SESSIONS.get(key)
        if not session:
            continue
        try:
            closed += int(await _close_registered(key, session))
        except Exception:
            logger.warning("Browser cleanup acknowledgement failed; retained for retry", exc_info=True)
    if closed:
        logger.info("Browser session sweep: %d expired lease(s) closed", closed)
    return closed
