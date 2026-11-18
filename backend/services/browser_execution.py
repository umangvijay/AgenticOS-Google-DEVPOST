"""Real browser execution service (Phase 20 §20.15).

Playwright-backed, against a LOCAL app or any allow-listed origin. Every
action is genuinely executed (navigation, click, typing, extraction,
screenshot) and recorded with executed=True evidence plus the actual
error on failure — the no-canned-output rule applies here exactly as it
does to model output. Ownership (§20.16) is enforced server-side on
every action: only the CURRENT controller epoch may act, and viewer
takeover is an explicit, audited transfer.
"""
from __future__ import annotations

import asyncio
import os
import logging
import time
import uuid
from dataclasses import dataclass, field
from functools import wraps
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger("browser_execution")

MAX_SCREENSHOT_BYTES = 2_000_000


class BrowserExecutionError(Exception):
    pass


class NotControllerError(BrowserExecutionError):
    """A non-owner attempted an action or takeover."""


@dataclass
class ExecutedAction:
    """Evidence record: what was asked, what ACTUALLY happened."""
    action: str
    target: str
    status: str                      # executed | failed
    executed: bool
    detail: str
    screenshot_sha256: Optional[str] = None
    url_after: Optional[str] = None
    duration_ms: int = 0
    error: Optional[str] = None


class StaleControllerError(BrowserExecutionError):
    """An action arrived under a STALE controller epoch (§15.20) —
    refused; the actor must re-resolve against the CURRENT epoch."""


@dataclass
class ManagedBrowserSession:
    session_id: str
    owner_user_id: str
    run_id: str
    task_id: str
    allowed_origins: List[str]
    controller_worker_id: str
    controller_epoch: str
    viewers: List[str] = field(default_factory=list)
    history: List[ExecutedAction] = field(default_factory=list)
    state: str = "running"          # running | paused | closed
    last_frame_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    requires_fresh_observation: bool = False

    # runtime handles (never serialized)
    _playwright: Any = None
    _browser: Any = None
    _page: Any = None
    _context: Any = None
    _owner_epoch: str = ""
    _controller_generation: int = 0

    @property
    def owner_generation(self) -> int:
        """§27.4: public generation access for the observation fence —
        the managed handle wraps the durable session's authority."""
        return self._controller_generation  # owner generation tracked on the durable row

    @property
    def controller_generation(self) -> int:
        return self._controller_generation
    _heartbeat: Any = None
    _action_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def serialized_session(method):
    """A handoff waits for the admitted action; stale queued actions recheck
    authority after acquiring the same lock. No await splits the check/act."""
    @wraps(method)
    async def wrapped(self, actor_user_id, session_id, *args, **kwargs):
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        async with session._action_lock:
            await self._assert_runtime_authority(session)
            return await method(self, actor_user_id, session_id, *args, **kwargs)
    return wrapped


def _origin_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        if not isinstance(url, str) or "\\" in url or any(
                ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError("invalid URL characters")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname \
                or parsed.username is not None or parsed.password is not None:
            raise ValueError("an HTTP(S) origin without credentials is required")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        default_port = 443 if parsed.scheme == "https" else 80
        suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parsed.scheme}://{host}{suffix}"
    except (ValueError, UnicodeError) as exc:
        raise BrowserExecutionError("invalid browser destination URL") from exc


class BrowserExecutionService:
    """Owns real Playwright sessions for run/task work, with
    server-enforced ownership and takeover (§20.16).

    §24.6 (R3): a session opened on THIS worker is served locally; a
    session whose durable registry row names ANOTHER worker is routed
    there over the internal HTTP contract — the OWNING worker applies
    the same server-side epoch/actor validation. If the owning worker
    is unreachable, the routing error says so honestly (never a fake
    local success)."""

    async def _route_remote(self, session_id: str, action: str,
                            body: dict) -> Optional[dict]:
        """Return the owning worker's response, or None when the session
        is local. Raises BrowserExecutionError honestly on routing
        failure."""
        if session_id in self._sessions:
            return None
        try:
            row = await self._registry_call("get_session", session_id)
        except Exception as exc:
            raise BrowserExecutionError("browser session registry unavailable") from exc
        if not row:
            return None
        owner_worker = (row.get("owner_worker_id") if isinstance(row, dict)
                        else getattr(row, "owner_worker_id", "")) or ""
        endpoint = (row.get("runtime_session_id") if isinstance(row, dict)
                    else getattr(row, "runtime_session_id", "")) or ""
        if owner_worker == self.worker_id or not endpoint:
            return None  # local or unroutable: serve locally/refuse locally
        trusted = os.environ.get("AGENTOS_BROWSER_TRUSTED_ENDPOINTS", "").split(",")
        if endpoint not in {url.strip() for url in trusted if url.strip()}:
            raise BrowserExecutionError("owning browser worker endpoint is not trusted")
        import httpx
        token = (os.environ.get("AGENTOS_BROWSER_INTERNAL_TOKEN")
                 or "").strip()
        if not token:
            raise BrowserExecutionError(
                "worker routing is not configured (missing "
                "AGENTOS_BROWSER_INTERNAL_TOKEN) — the session lives on "
                f"worker {owner_worker!r}")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{endpoint}/internal/browser/{action}",
                    json={"session_id": session_id, **body},
                    headers={"x-internal-token": token})
        except Exception as exc:  # noqa: BLE001
            raise BrowserExecutionError(
                f"the owning browser worker ({owner_worker}) is "
                f"unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            detail = resp.json().get("detail", "") if resp.headers.get(
                "content-type", "").startswith("application/json") else ""
            if resp.status_code == 403:
                raise NotControllerError(f"owning worker denied {action}")
            raise BrowserExecutionError(
                f"owning worker refused {action}: "
                f"{resp.status_code} {detail[:200]}")
        return resp.json()

    def __init__(self, *, headless: bool = True, allow_private_network: bool = False,
                 worker_id: str = "", internal_endpoint: str = ""):
        self._headless = headless
        self._allow_private_network = allow_private_network
        self._sessions: Dict[str, ManagedBrowserSession] = {}
        # §24.6 (R3): worker identity + the address OTHER replicas use to
        # route commands to the worker that owns a live session.
        import os
        from backend.services.capacity_service import WORKER_ID
        self.worker_id = worker_id or os.environ.get("AGENTOS_BROWSER_WORKER_ID", WORKER_ID)
        self.internal_endpoint = internal_endpoint or os.environ.get(
            "AGENTOS_BROWSER_INTERNAL_URL", "")
        self._session_repo = None
        self._heartbeat_interval = 20

    async def adopt_session(self, durable, *, playwright, browser, context, page):
        """Expose the WebAgent's actual handles; never launch a second browser."""
        from backend.models.browser_session import ControllerState
        existing = self._sessions.get(durable.session_id)
        if existing:
            if existing._page is not page or existing._owner_epoch != durable.owner_epoch:
                raise BrowserExecutionError("session is bound to different runtime handles")
            await self._assert_runtime_authority(existing)
            return existing
        if durable.owner_worker_id != self.worker_id:
            raise BrowserExecutionError("only the owning worker can expose a browser")
        row = await self._registry_call("change_runtime_control", durable.session_id,
            self.worker_id, durable.owner_epoch, durable.controller_generation,
            ControllerState.AGENT, endpoint=self.internal_endpoint or None)
        if row is None:
            raise StaleControllerError("browser registration lost its owner lease or generation")
        session = ManagedBrowserSession(
            session_id=durable.session_id, owner_user_id=durable.user_id,
            run_id=durable.run_id or "", task_id=durable.task_id or "",
            allowed_origins=[_origin_of(u if "://" in u else "https://" + u) for u in durable.allowed_origins],
            controller_worker_id="agent", controller_epoch=f"{row.owner_epoch}:{row.controller_generation}",
            _owner_epoch=row.owner_epoch, _controller_generation=row.controller_generation,
            _playwright=playwright, _browser=browser, _context=context, _page=page)
        self._sessions[session.session_id] = session
        session._heartbeat = asyncio.create_task(self._renew_runtime(session))
        return session

    async def _assert_runtime_authority(self, session):
        if session.state in ("closed", "closing"):
            raise BrowserExecutionError("browser is closing or closed")
        if not session._owner_epoch:  # legacy standalone viewer
            return
        from backend.models.browser_session import ResourceState, ControllerState
        row = await self._registry_call("get_session", session.session_id)
        expected_control = (ControllerState.NONE if session.state == "paused" else
                            ControllerState.HUMAN if session.controller_worker_id.startswith("user:") else ControllerState.AGENT)
        if (row is None or row.owner_worker_id != self.worker_id
                or row.owner_epoch != session._owner_epoch or row.lease_expires_at <= time.time()
                or row.resource_state is not ResourceState.READY
                or row.controller_generation != session._controller_generation
                or row.controller_state is not expected_control):
            raise StaleControllerError("browser ownership, lease or controller generation changed")

    async def _persist_control(self, session, controller):
        if not session._owner_epoch:
            return
        row = await self._registry_call("change_runtime_control", session.session_id,
            self.worker_id, session._owner_epoch, session._controller_generation, controller)
        if row is None:
            raise StaleControllerError("control transfer lost its generation or owner lease")
        session._controller_generation = row.controller_generation

    async def _renew_runtime(self, session):
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                async with session._action_lock:
                    await self._assert_runtime_authority(session)
                    renewed = await self._registry_call("renew_ownership", session.session_id,
                        self.worker_id, session._owner_epoch, 120)
                    if not renewed:
                        raise StaleControllerError("browser lease renewal refused")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Browser ownership lost; terminating local runtime", exc_info=True)
            async with session._action_lock:
                session.state = "closed"
                await self._close(session)

    @asynccontextmanager
    async def agent_observation(self, session_id, *, timeout=600):
        """Pause the autonomous loop during human control without closing its page."""
        session = self._require(session_id)
        deadline = time.monotonic() + timeout
        while True:
            async with session._action_lock:
                await self._assert_runtime_authority(session)
                if session.state == "closed":
                    raise BrowserExecutionError("browser closed")
                if session.state == "running" and session.controller_worker_id == "agent":
                    yield session
                    session.requires_fresh_observation = False
                    return
            if time.monotonic() >= deadline:
                raise BrowserExecutionError("human control handoff deadline expired")
            await asyncio.sleep(0.1)

    @asynccontextmanager
    async def agent_dispatch(self, session_id, controller_epoch):
        session = self._require(session_id)
        async with session._action_lock:
            await self._assert_runtime_authority(session)
            if (session.state != "running" or session.controller_worker_id != "agent"
                    or session.controller_epoch != controller_epoch or session.requires_fresh_observation):
                raise StaleControllerError("agent decision predates control transfer; re-observe")
            yield session

    async def detach_session(self, session_id):
        session = self._sessions.pop(session_id, None)
        if session and session._heartbeat:
            session._heartbeat.cancel()
            await asyncio.gather(session._heartbeat, return_exceptions=True)

    def _durable_repo(self):
        """Use the process repository initialized by the active storage profile."""
        if self._session_repo is not None:
            return self._session_repo
        # The factory binds the configured PostgreSQL/SQLite/Firestore repo.
        injected = None
        try:
            from backend.repositories.browser_session_repository import (
                get_browser_session_repo)
            injected = get_browser_session_repo()
        except Exception:
            injected = None
        if injected is not None:
            return injected
        return None

    async def _registry_call(self, method, *args, **kwargs):
        repo = self._durable_repo()
        if repo is not None:
            return await getattr(repo, method)(*args, **kwargs)
        # Standalone services can share a disk registry. Scope its connection
        # to the operation so worker event loops never share SQLite handles.
        from backend.repositories.sqlite.database import DatabaseManager
        from backend.repositories.browser_session_repository import BrowserSessionRepository
        db = DatabaseManager(os.environ.get("BROWSER_SESSION_DB", "data/browser_sessions.db"))
        try:
            await db.initialize()
            return await getattr(BrowserSessionRepository(db), method)(*args, **kwargs)
        finally:
            await db.close()

    async def _register_durable(self, session) -> None:
        """Persist the session with its OWNING worker so any replica can
        route commands here. Registration must succeed before exposure."""
        if self.internal_endpoint:
            await self._registry_call(
                "register_worker_session", session_id=session.session_id,
                worker_id=self.worker_id, endpoint=self.internal_endpoint,
                owner_user_id=session.owner_user_id, run_id=session.run_id,
                task_id=session.task_id, controller_epoch=session.controller_epoch)

    async def open_session(self, *, owner_user_id: str, run_id: str,
                           task_id: str, allowed_origins: List[str],
                           controller_worker_id: str = "agent") -> ManagedBrowserSession:
        if not allowed_origins:
            raise BrowserExecutionError(
                "no allowed origins — scope is unbounded (refused)")
        for origin in allowed_origins:
            await self._validate_destination(origin)
        from playwright.async_api import async_playwright
        session = ManagedBrowserSession(
            session_id=f"bs-{uuid.uuid4().hex[:12]}",
            owner_user_id=owner_user_id,
            run_id=run_id, task_id=task_id,
            allowed_origins=list(dict.fromkeys(_origin_of(u) for u in allowed_origins)),
            controller_worker_id=controller_worker_id,
            controller_epoch=f"epoch-{uuid.uuid4().hex[:8]}")
        session._playwright = await async_playwright().start()
        try:
            session._browser = await session._playwright.chromium.launch(
                headless=self._headless)
            context = await session._browser.new_context(service_workers="block")

            async def guard(route):
                await self._guard_request(session, route)

            # Context routing covers redirects, frames, popups, forms and
            # subresources; validating only page.goto misses those requests.
            await context.route("**/*", guard)

            async def deny_websocket(websocket):
                # Socket traffic requires its own grant/validation contract.
                await websocket.close()

            await context.route_web_socket("**/*", deny_websocket)
            session._page = await context.new_page()
        except BaseException:
            await self._close(session)
            raise
        try:
            await self._register_durable(session)
        except Exception as exc:
            await self._close(session)
            raise BrowserExecutionError("unable to register owning browser worker") from exc
        self._sessions[session.session_id] = session
        return session

    async def close_session(self, actor_user_id: str, session_id: str) -> None:
        remote = await self._route_remote(session_id, "close", {"actor_user_id": actor_user_id})
        if remote is not None:
            return
        return await self._close_session_local(actor_user_id, session_id)

    @serialized_session
    async def _close_session_local(self, actor_user_id: str, session_id: str) -> None:
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        if not await self._close(session):
            raise BrowserExecutionError("browser cleanup failed; runtime retained for retry")
        if self.internal_endpoint and not session._owner_epoch:
            await self._registry_call("close_session", session_id)
        self._sessions.pop(session_id, None)
        session.state = "closed"

    async def _close(self, session: ManagedBrowserSession) -> bool:
        if session._heartbeat and session._heartbeat is not asyncio.current_task():
            session._heartbeat.cancel()
            await asyncio.gather(session._heartbeat, return_exceptions=True)
        complete = True
        for closer in (session._context or session._page, session._browser, session._playwright):
            if closer is None:
                continue
            try:
                if closer is not session._playwright:
                    await closer.close()
                else:
                    await closer.stop()
            except Exception:
                complete = False
                logger.warning("browser close failed", exc_info=True)
        if session._owner_epoch:
            await self._registry_call("acknowledge_cleanup", session.session_id,
                self.worker_id, session._owner_epoch, complete=complete)
        return complete

    # ── ownership (§20.16) ─────────────────────────────────────────────

    @staticmethod
    def _require_controller(session: ManagedBrowserSession,
                            actor_user_id: str) -> None:
        if session.owner_user_id != actor_user_id:
            raise NotControllerError(
                f"user {actor_user_id} does not control session "
                f"{session.session_id}")

    def get_session(self, session_id: str) -> ManagedBrowserSession:
        return self._require(session_id)

    async def session_state_for(self, actor_user_id: str,
                                session_id: str) -> Dict[str, Any]:
        """§25.3: full session state, routed across replicas first."""
        remote = await self._route_remote(session_id, "state",
                                          {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        session = self._require(session_id)
        if actor_user_id != session.owner_user_id and \
                actor_user_id not in session.viewers:
            raise NotControllerError(
                f"user {actor_user_id} does not control session "
                f"{session.session_id}")
        return {
            "session_id": session.session_id,
            "owner_user_id": session.owner_user_id,
            "controller": session.controller_worker_id,
            "controller_epoch": session.controller_epoch,
            "requires_fresh_observation": session.requires_fresh_observation,
            "url": session._page.url if session._page else None,
            "role": ("controller"
                     if session.controller_worker_id == f"user:{actor_user_id}"
                     else "viewer" if actor_user_id in session.viewers
                     else "observer"),
            "history": [
                {"action": h.action, "target": h.target, "status": h.status,
                 "executed": h.executed, "detail": h.detail[:500],
                 "error": h.error}
                for h in session.history[-50:]],
        }

    async def list_sessions(self, actor_user_id: str) -> List[Dict[str, Any]]:
        """§25.3: the workspace session list must discover sessions the
        user owns ACROSS replicas — local sessions plus the durable
        registry's rows for this owner (each marked with its worker)."""
        local = [
            {"session_id": s.session_id, "run_id": s.run_id,
             "task_id": s.task_id, "url": s._page.url if s._page else None,
             "viewer": actor_user_id in s.viewers,
             "controller": s.controller_worker_id, "worker": self.worker_id}
            for s in self._sessions.values()
            if s.owner_user_id == actor_user_id]
        seen = {r["session_id"] for r in local}
        try:
            rows = await self._registry_call("list_for_owner", actor_user_id)
        except Exception:
            rows = []
        for row in rows:
            sid = (row.get("session_id") if isinstance(row, dict)
                   else getattr(row, "session_id", ""))
            if not sid or sid in seen:
                continue
            seen.add(sid)
            local.append({
                "session_id": sid,
                "run_id": (row.get("run_id") if isinstance(row, dict)
                           else getattr(row, "run_id", "")),
                "task_id": (row.get("task_id") if isinstance(row, dict)
                            else getattr(row, "task_id", "")),
                "url": None,
                "viewer": True,
                "controller": (row.get("owner_worker_id")
                               if isinstance(row, dict)
                               else getattr(row, "owner_worker_id", "")),
                "worker": (row.get("owner_worker_id")
                           if isinstance(row, dict)
                           else getattr(row, "owner_worker_id", "")),
            })
        return local

    async def begin_viewing(self, actor_user_id: str, session_id: str) -> Dict[str, Any]:
        """A viewer attaches READ-ONLY; viewing never grants control."""
        remote = await self._route_remote(session_id, "view",
                                          {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        if actor_user_id not in session.viewers:
            session.viewers.append(actor_user_id)
        return {"session_id": session.session_id,
                "mode": "viewing", "url": session._page.url}

    async def request_takeover(self, actor_user_id: str,
                               session_id: str) -> Dict[str, Any]:
        """§20.16 takeover: SERVER-ENFORCED ownership transfer. Routing
        happens BEFORE the local lock/lookup (§25.3)."""
        remote = await self._route_remote(session_id, "takeover",
                                          {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        return await self._request_takeover_local(actor_user_id, session_id)

    @serialized_session
    async def _request_takeover_local(self, actor_user_id: str,
                                      session_id: str) -> Dict[str, Any]:
        """The current controller's epoch is invalidated — subsequent
        actions under the old epoch are refused — and the takeover is
        recorded in the session history."""
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        old_epoch = session.controller_epoch
        from backend.models.browser_session import ControllerState
        await self._persist_control(session, ControllerState.HUMAN)
        session.controller_epoch = f"epoch-{uuid.uuid4().hex[:8]}"
        session.controller_worker_id = f"user:{actor_user_id}"
        session.requires_fresh_observation = False
        if session.state == "paused":
            session.state = "running"
        session.history.append(ExecutedAction(
            action="takeover", target=session.session_id, status="executed",
            executed=True,
            detail=f"control moved to user:{actor_user_id} "
                   f"(epoch {old_epoch} → {session.controller_epoch})"))
        return {"session_id": session.session_id,
                "controller_epoch": session.controller_epoch,
                "controller": session.controller_worker_id}

    async def pause(self, actor_user_id: str, session_id: str) -> Dict[str, Any]:
        remote = await self._route_remote(session_id, "pause", {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        return await self._pause_local(actor_user_id, session_id)

    @serialized_session
    async def _pause_local(self, actor_user_id: str, session_id: str) -> Dict[str, Any]:
        """Server-enforced pause: agent actions are REFUSED while paused
        (execute() gates on this); the user holds the live view."""
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        from backend.models.browser_session import ControllerState
        await self._persist_control(session, ControllerState.NONE)
        session.controller_epoch = f"epoch-{uuid.uuid4().hex[:8]}"
        session.state = "paused"
        session.history.append(ExecutedAction(
            action="pause", target=session.session_id, status="executed",
            executed=True, detail="agent actions gated server-side"))
        return {"session_id": session.session_id, "state": "paused"}

    async def resume(self, actor_user_id: str, session_id: str) -> Dict[str, Any]:
        remote = await self._route_remote(session_id, "resume", {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        return await self._resume_local(actor_user_id, session_id)

    @serialized_session
    async def _resume_local(self, actor_user_id: str, session_id: str) -> Dict[str, Any]:
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        if session.state == "paused":
            from backend.models.browser_session import ControllerState
            await self._persist_control(session, ControllerState.HUMAN if session.controller_worker_id.startswith("user:") else ControllerState.AGENT)
            session.controller_epoch = f"epoch-{uuid.uuid4().hex[:8]}"
            session.requires_fresh_observation = True
            session.state = "running"
            session.history.append(ExecutedAction(
                action="resume", target=session.session_id,
                status="executed", executed=True,
                detail="agent actions allowed again"))
        return {"session_id": session.session_id, "state": session.state,
                "controller_epoch": session.controller_epoch,
                "requires_fresh_observation": session.requires_fresh_observation}

    async def return_control(self, actor_user_id: str,
                             session_id: str) -> Dict[str, Any]:
        """§25.3: route remote FIRST (before any local lock); the local
        impl holds the session lock exactly once. The Phase 25 audit
        found the previous wrapper acquiring the same asyncio lock twice
        — a guaranteed hang on the local path."""
        remote = await self._route_remote(session_id, "return-control",
                                          {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        return await self._return_control_local(actor_user_id, session_id)

    @serialized_session
    async def _return_control_local(self, actor_user_id: str,
                                    session_id: str) -> Dict[str, Any]:
        """The human hands the session back to the agent: the epoch
        rotates again so the agent's NEXT action must be a FRESH
        observation (§21.13 acceptance) — its old epoch is fenced."""
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        if session.controller_worker_id != f"user:{actor_user_id}":
            raise NotControllerError("take control before returning it")
        old = session.controller_epoch
        from backend.models.browser_session import ControllerState
        await self._persist_control(session, ControllerState.AGENT)
        session.controller_epoch = f"epoch-{uuid.uuid4().hex[:8]}"
        session.controller_worker_id = "agent"
        session.requires_fresh_observation = True
        session.history.append(ExecutedAction(
            action="return_control", target=session.session_id,
            status="executed", executed=True,
            detail=f"control returned to the agent "
                   f"(epoch {old} → {session.controller_epoch}); "
                   "the agent must re-observe before acting"))
        return {"session_id": session.session_id,
                "controller_epoch": session.controller_epoch,
                "controller": "agent",
                "requires_fresh_observation": True}

    async def capture_frame(self, actor_user_id: str,
                            session_id: str) -> Dict[str, Any]:
        """THE LIVE VIEW (§21.13): routed across replicas first (§25.3);
        locally it is a REAL screenshot of the agent's actual page."""
        remote = await self._route_remote(session_id, "frame",
                                          {"actor_user_id": actor_user_id})
        if remote is not None:
            return remote
        return await self._capture_frame_local(actor_user_id, session_id)

    @serialized_session
    async def _capture_frame_local(self, actor_user_id: str,
                                   session_id: str) -> Dict[str, Any]:
        import base64
        session = self._require(session_id)
        if session.owner_user_id != actor_user_id and \
                actor_user_id not in session.viewers:
            raise NotControllerError(
                f"user {actor_user_id} does not control session "
                f"{session.session_id}")
        if session._page is None:
            raise BrowserExecutionError("session has no live page")
        shot = await session._page.screenshot(timeout=15000)
        if len(shot) > MAX_SCREENSHOT_BYTES:
            raise BrowserExecutionError("screenshot exceeds cap")
        import hashlib
        session.last_frame_at = time.time()
        return {
            "session_id": session.session_id,
            "url": session._page.url,
            "state": session.state,
            "controller": session.controller_worker_id,
            "controller_epoch": session.controller_epoch,
            "freshness": session.last_frame_at,
            "screenshot_sha256": hashlib.sha256(shot).hexdigest(),
            "frame_base64": base64.b64encode(shot).decode(),
        }

    # ── real actions ───────────────────────────────────────────────────

    def _require(self, session_id: str) -> ManagedBrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise BrowserExecutionError(
                f"browser session {session_id} does not exist")
        return session

    def _assert_origin_allowed(self, session: ManagedBrowserSession,
                               url: str) -> None:
        origin = _origin_of(url)
        if origin not in {_origin_of(u) for u in session.allowed_origins}:
            raise BrowserExecutionError(
                f"origin {origin} is outside the session's allowed "
                f"origins {session.allowed_origins}")

    async def _guard_request(self, session: ManagedBrowserSession, route) -> None:
        try:
            self._assert_origin_allowed(session, route.request.url)
            await self._validate_destination(route.request.url)
        except BrowserExecutionError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _validate_destination(self, url: str) -> None:
        """Reject private DNS answers at admission and each HTTP request.

        Network-level egress enforcement is still required against DNS
        rebinding between validation and the browser's own resolution.
        The private-network opt-in is used only by controlled local tests.
        """
        import ipaddress
        import socket
        from urllib.parse import urlsplit
        origin = _origin_of(url)
        if self._allow_private_network:
            return
        parsed = urlsplit(origin)
        host = parsed.hostname.rstrip(".")
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local")):
            raise BrowserExecutionError("private browser destinations are prohibited")
        try:
            answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM), timeout=5)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BrowserExecutionError("browser destination could not be validated") from exc
        if not answers:
            raise BrowserExecutionError("browser destination did not resolve")
        for answer in answers:
            address = ipaddress.ip_address(answer[4][0].split("%")[0])
            mapped = getattr(address, "ipv4_mapped", None)
            if not address.is_global or (mapped is not None and not mapped.is_global):
                raise BrowserExecutionError("private browser destinations are prohibited")

    async def execute(self, actor_user_id: str, session_id: str,
                      action: str, target: str = "",
                      text: str = "",
                      controller_epoch: Optional[str] = None,
                      actor_controller_id: str = "agent") -> ExecutedAction:
        """Execute ONE real browser action and return its evidence.

        Enforcement (§21.13): the caller must OWN the session, hold the
        CURRENT controller epoch (stale epochs are REFUSED after a
        takeover), and the session must not be paused — agent activity
        is gated server-side, not by convention."""
        remote = await self._route_remote(session_id, "execute", {
            "actor_user_id": actor_user_id, "action": action,
            "target": target, "text": text,
            "controller_epoch": controller_epoch or "",
            "actor_controller_id": actor_controller_id})
        if remote is not None:
            return ExecutedAction(**remote)
        return await self._execute_local(
            actor_user_id, session_id, action, target=target, text=text,
            controller_epoch=controller_epoch,
            actor_controller_id=actor_controller_id)

    @serialized_session
    async def _execute_local(self, actor_user_id: str, session_id: str,
                             action: str, target: str = "",
                             text: str = "",
                             controller_epoch: Optional[str] = None,
                             actor_controller_id: str = "agent") -> ExecutedAction:
        session = self._require(session_id)
        self._require_controller(session, actor_user_id)
        if not controller_epoch or controller_epoch != session.controller_epoch:
            raise StaleControllerError(
                f"stale controller epoch for session {session.session_id} "
                "— re-resolve before acting")
        if actor_controller_id != session.controller_worker_id:
            raise NotControllerError("caller does not hold browser control")
        if session.requires_fresh_observation and action not in ("extract_text", "screenshot"):
            raise BrowserExecutionError("fresh observation required after control handoff")
        if session.state == "paused" and action not in ("takeover", "wait"):
            raise BrowserExecutionError(
                f"session {session.session_id} is PAUSED — agent actions "
                "are refused until resumed")
        if session.state == "closed":
            raise BrowserExecutionError(
                f"session {session.session_id} is closed")
        page = session._page
        started = time.time()
        try:
            if action == "goto":
                self._assert_origin_allowed(session, target)
                await self._validate_destination(target)
                await page.goto(target, wait_until="domcontentloaded",
                                timeout=15000)
                result = ExecutedAction(
                    action=action, target=target, status="executed",
                    executed=True, detail=f"navigated to {page.url}",
                    url_after=page.url)
            elif action == "click":
                await page.click(target, timeout=10000)
                result = ExecutedAction(
                    action=action, target=target, status="executed",
                    executed=True, detail=f"clicked {target}",
                    url_after=page.url)
            elif action == "type":
                await page.fill(target, text, timeout=10000)
                result = ExecutedAction(
                    action=action, target=target, status="executed",
                    executed=True,
                    detail=f"typed {len(text)} chars into {target}",
                    url_after=page.url)
            elif action == "click_at":
                import json
                import math
                point = json.loads(target)
                x, y = float(point["x"]), float(point["y"])
                viewport = page.viewport_size
                if not viewport or not all(math.isfinite(n) for n in (x, y)) or not (
                        0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
                    raise BrowserExecutionError("click is outside the browser viewport")
                await page.mouse.click(x, y)
                result = ExecutedAction(action, target, "executed", True,
                                        "clicked browser viewport", url_after=page.url)
            elif action == "insert_text":
                if len(text) > 4000:
                    raise BrowserExecutionError("input exceeds limit")
                await page.keyboard.insert_text(text)
                result = ExecutedAction(action, "focused element", "executed", True,
                                        f"entered {len(text)} characters", url_after=page.url)
            elif action == "press_key":
                if target not in {"Enter", "Tab", "Shift+Tab", "Escape", "Backspace",
                                  "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"}:
                    raise BrowserExecutionError("unsupported browser key")
                await page.keyboard.press(target)
                result = ExecutedAction(action, target, "executed", True,
                                        "pressed browser key", url_after=page.url)
            elif action == "extract_text":
                content = await page.inner_text("body", timeout=10000)
                result = ExecutedAction(
                    action=action, target=target or "body",
                    status="executed", executed=True,
                    detail=content[:2000], url_after=page.url)
            elif action == "screenshot":
                shot = await page.screenshot(timeout=15000)
                import hashlib
                if len(shot) > MAX_SCREENSHOT_BYTES:
                    raise BrowserExecutionError("screenshot exceeds cap")
                result = ExecutedAction(
                    action=action, target=target or page.url,
                    status="executed", executed=True,
                    detail=f"{len(shot)} bytes captured",
                    screenshot_sha256=hashlib.sha256(shot).hexdigest(),
                    url_after=page.url)
            elif action == "wait":
                await page.wait_for_timeout(min(int(target or 100), 5000))
                result = ExecutedAction(
                    action=action, target=target, status="executed",
                    executed=True, detail=f"waited {target}ms")
            else:
                raise BrowserExecutionError(
                    f"unknown action {action!r} — refused, not guessed")
        except BrowserExecutionError:
            raise
        except Exception as exc:
            # REAL failure recorded as evidence — never rewritten to success.
            result = ExecutedAction(
                action=action, target=target, status="failed",
                executed=True, detail="",
                error=str(exc)[:500],
                duration_ms=int((time.time() - started) * 1000))
            session.history.append(result)
            return result
        result.duration_ms = int((time.time() - started) * 1000)
        if action in ("extract_text", "screenshot"):
            session.requires_fresh_observation = False
        session.history.append(result)
        return result

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await self._close(session)
        self._sessions.clear()
