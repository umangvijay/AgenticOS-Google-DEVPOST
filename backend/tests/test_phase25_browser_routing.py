"""Phase 25 §25.3 — browser return-control and cross-replica routing.

The audit reproduced a LOCAL return-control hang without Chromium: the
wrapper held the session lock and the local impl re-acquired it. These
regressions are BOUNDED — a recurrence fails in seconds, never hangs.
"""
import asyncio
import json
import os
import uuid

import pytest


@pytest.mark.asyncio
async def test_return_control_local_completes_quickly():
    """THE deadlock regression: local return-control must complete well
    inside a 5-second bound (the double-lock bug hung forever)."""
    from backend.services.browser_execution import BrowserExecutionService
    svc = BrowserExecutionService(headless=True, worker_id="w-return")

    # A session WITHOUT chromium: the authority/lock machinery is what
    # deadlocked; _page stays None so no browser is needed.
    class _Ses:
        _owner_epoch = ""
        session_id = "bs-return-test"
        owner_user_id = "owner-u"
        run_id = "r"
        task_id = "t"
        allowed_origins = ["https://example.com"]
        controller_worker_id = "user:owner-u"
        controller_epoch = "epoch-1"
        viewers = []
        history = []
        requires_fresh_observation = False
        _action_lock = asyncio.Lock()
        _page = None
        state = "READY"
        paused = False

    ses = _Ses()
    svc._sessions[ses.session_id] = ses

    # The registry is absent here: routing must fall through honestly to
    # the local path (a missing row is not a routing error).
    result = await asyncio.wait_for(
        svc.return_control("owner-u", ses.session_id), timeout=5.0)
    assert result["requires_fresh_observation"] is True
    assert ses.controller_epoch != "epoch-1", "the epoch must rotate"
    assert ses.controller_worker_id == "agent", "the agent controls again"


@pytest.mark.asyncio
async def test_takeover_and_stale_refusal_local():
    from backend.services.browser_execution import (
        BrowserExecutionService, StaleControllerError)
    svc = BrowserExecutionService(headless=True, worker_id="w-take")

    class _Ses:
        _owner_epoch = ""
        session_id = "bs-take-test"
        owner_user_id = "owner-u"
        run_id = "r"
        task_id = "t"
        allowed_origins = ["https://example.com"]
        controller_worker_id = "user:owner-u"
        controller_epoch = "epoch-a"
        viewers = []
        history = []
        requires_fresh_observation = False
        _action_lock = asyncio.Lock()
        _page = None
        state = "READY"
        paused = False

    ses = _Ses()
    svc._sessions[ses.session_id] = ses
    taken = await asyncio.wait_for(
        svc.request_takeover("owner-u", ses.session_id), timeout=5.0)
    new_epoch = taken["controller_epoch"]
    assert new_epoch != "epoch-a"
    with pytest.raises(StaleControllerError):
        await asyncio.wait_for(
            svc.execute("owner-u", ses.session_id, "click", target="#h",
                        controller_epoch="epoch-a"), timeout=5.0)
    handed = await asyncio.wait_for(
        svc.return_control("owner-u", ses.session_id), timeout=5.0)
    assert handed["requires_fresh_observation"] is True
    # After return, the ORIGINAL epoch is fenced and the agent must
    # re-observe: the agent's next action uses a NEW epoch.
    assert ses.controller_epoch not in (new_epoch, "epoch-a")


@pytest.mark.asyncio
async def test_remote_routing_sends_the_internal_token(monkeypatch):
    """The forwarding client must present the shared token; the receiver
    refuses missing/incorrect credentials (§25.3)."""
    from backend.services.browser_execution import BrowserExecutionService
    sent = {}

    class _Repo:
        async def get_session(self, session_id):
            return {"session_id": session_id, "owner_worker_id": "worker-A",
                    "runtime_session_id": "http://worker-a.internal:9"}

    monkeypatch.setenv("AGENTOS_BROWSER_TRUSTED_ENDPOINTS", "http://worker-a.internal:9")
    monkeypatch.setenv("AGENTOS_BROWSER_INTERNAL_TOKEN", "tok-123")
    svc = BrowserExecutionService(headless=True, worker_id="worker-B")
    svc._session_repo = _Repo()

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"mode": "viewing"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["token"] = (headers or {}).get("x-internal-token")
            return _Resp()

    import backend.services.browser_execution as be
    real_httpx = be.httpx if hasattr(be, "httpx") else None
    # patch httpx used inside the method
    import httpx as real_httpx_mod
    monkeypatch.setattr(real_httpx_mod, "AsyncClient", _Client)
    out = await svc.begin_viewing("owner-u", "bs-remote-1")
    assert out == {"mode": "viewing"}
    assert sent["token"] == "tok-123", "the routing client must authenticate"


@pytest.mark.asyncio
async def test_receiver_refuses_wrong_token(monkeypatch):
    """The internal endpoint requires the configured token; a wrong or
    missing token is a 403/503, never a pass-through."""
    monkeypatch.setenv("AGENTOS_BROWSER_INTERNAL_TOKEN", "tok-correct")
    from backend.api.routers import browser_viewer
    from fastapi import HTTPException
    import httpx

    request = type("R", (), {"headers": {"x-internal-token": "tok-wrong"}})()
    with pytest.raises(HTTPException) as exc:
        browser_viewer._require_internal_token(request)
    assert exc.value.status_code == 403

    request2 = type("R", (), {"headers": {}})()
    with pytest.raises(HTTPException) as exc2:
        browser_viewer._require_internal_token(request2)
    assert exc2.value.status_code == 403


@pytest.mark.asyncio
async def test_unreachable_owning_worker_reports_honestly(monkeypatch):
    from backend.services.browser_execution import (
        BrowserExecutionService, BrowserExecutionError)

    class _Repo:
        async def get_session(self, session_id):
            return {"session_id": session_id, "owner_worker_id": "worker-A",
                    "runtime_session_id": "http://127.0.0.1:1"}

    monkeypatch.setenv("AGENTOS_BROWSER_TRUSTED_ENDPOINTS", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTOS_BROWSER_INTERNAL_TOKEN", "tok")
    svc = BrowserExecutionService(headless=True, worker_id="worker-B")
    svc._session_repo = _Repo()
    with pytest.raises(BrowserExecutionError) as exc:
        await asyncio.wait_for(
            svc.begin_viewing("owner-u", "bs-gone"), timeout=15)
    assert "unreachable" in str(exc.value)
