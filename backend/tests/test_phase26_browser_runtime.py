"""Real Chromium on an intercepted HTTPS fixture; no public application traffic."""
from unittest.mock import AsyncMock

import pytest

from backend.tests.test_phase26_web_agent_safety import runtime  # noqa: F401
from backend.services import browser_sessions as local
from backend.services.web_agent import WebAgent, WebAgentError


@pytest.mark.asyncio
async def test_real_chromium_refuses_reload_then_runs_click_with_checkpoint(runtime):
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    pw = await async_playwright().start()
    browser = None
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="block")
        await context.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body='''<title>Phase 26</title>
            <button type="button" onclick="document.querySelector('p').textContent='Details loaded'">Details</button>
            <p>Waiting</p>'''))
        page = await context.new_page()
        await page.goto("https://example.com/")
        agent = WebAgent()
        snapshot = await agent._snapshot(page)
        await page.reload()
        decision = {"action": "click", "index": 0}
        kind, args = agent._action_arguments(decision)
        with pytest.raises(WebAgentError, match="document changed"):
            await agent._validate_dispatch(page, snapshot, decision, {"example.com"}, repo,
                                           owned, kind, args, consequential=False, effect_recorded=False)
        local.put_session("run", "task", local.BrowserSession(
            playwright=pw, browser=browser, context=context, page=page,
            allowed_domains={"example.com"}, secrets={}, goal="Open details", history=[], steps=[],
            step_num=0, max_steps=2, challenge_type="", url=page.url,
            extra={"durable_session_id": "session", "owner_epoch": "epoch"}))
        async def decide(goal, observed, *args):
            if "Details loaded" in observed["text"]:
                return {"action": "done", "success": True, "result": "Details loaded"}
            return decision
        agent._decide = AsyncMock(side_effect=decide)
        result = await agent.run("Open details", page.url, "user", run_id="run", task_id="task")
        assert result["success"] is True
        assert result["steps"][0]["executed"] is True
        assert result["checkpoint"]["reference"]
        assert result["browser_evidence"]["executed_step_count"] == 1
        assert not browser.is_connected()
    finally:
        if browser and browser.is_connected():
            await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_webagent_viewer_share_page_and_fence_takeover_decision(runtime):
    import asyncio
    from backend.api.routers.browser_viewer import get_browser_service
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(service_workers="block")
    await context.route("**/*", lambda route: route.fulfill(content_type="text/html", body='''
        <button type="button" onclick="document.querySelector('p').textContent=String(++window.clicks)">Details</button>
        <p>0</p><script>window.clicks=0</script>'''))
    page = await context.new_page()
    await page.goto("https://example.com/")
    local.put_session("run", "task", local.BrowserSession(
        playwright=pw, browser=browser, context=context, page=page,
        allowed_domains={"example.com"}, secrets={}, goal="Inspect twice", history=[], steps=[],
        step_num=0, max_steps=3, challenge_type="", url=page.url,
        extra={"durable_session_id": "session", "owner_epoch": "epoch"}))
    agent = WebAgent()
    proposed, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def decide(goal, observed, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            proposed.set()
            await release.wait()
        if calls <= 2:
            return {"action": "click", "index": 0}
        assert "2" in observed["text"]
        return {"action": "done", "success": True, "result": "Two observed interactions"}
    agent._decide = decide
    agent._execute = AsyncMock(wraps=agent._execute)
    task = asyncio.create_task(agent.run("Inspect twice", page.url, "user", run_id="run", task_id="task"))
    service = get_browser_service()
    try:
        await asyncio.wait_for(proposed.wait(), 10)
        managed = service.get_session("session")
        assert managed._page is page
        assert managed._browser is browser
        frame = await service.capture_frame("user", "session")
        assert frame["frame_base64"]
        old_epoch = managed.controller_epoch
        takeover = await service.request_takeover("user", "session")
        await service.execute("user", "session", "click", "button", controller_epoch=takeover["controller_epoch"],
                              actor_controller_id="user:user")
        assert await page.locator("p").inner_text() == "1"
        await service.return_control("user", "session")
        assert managed.controller_epoch != old_epoch
        assert managed.requires_fresh_observation
        release.set()
        result = await asyncio.wait_for(task, 15)
        assert result["success"]
        assert result["steps"][0]["executed"] is False
        assert agent._execute.await_count == 1
        assert not browser.is_connected()
        assert "session" not in service._sessions
        final = await repo.get_session("session")
        assert final.controller_generation == 3
        assert final.cleanup_state.value == "COMPLETE"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if browser.is_connected():
            await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_actual_webagent_page_is_routed_through_worker_http(runtime, monkeypatch):
    import asyncio
    import socket
    import secrets
    import httpx
    from backend.api.routers.browser_viewer import get_browser_service
    from backend.services.browser_execution import BrowserExecutionService
    from backend.services.browser_runtime_server import browser_runtime_server
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AGENTOS_BROWSER_INTERNAL_URL", endpoint)
    monkeypatch.setenv("AGENTOS_BROWSER_INTERNAL_TOKEN", secrets.token_hex(32))
    monkeypatch.setenv("AGENTOS_BROWSER_TRUSTED_ENDPOINTS", endpoint)
    monkeypatch.setenv("AGENTOS_BROWSER_BIND_HOST", "127.0.0.1")
    service = get_browser_service()
    monkeypatch.setattr(service, "internal_endpoint", endpoint)
    other = BrowserExecutionService(worker_id="api-replica")
    other._session_repo = repo
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(service_workers="block")
    await context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<p>Shared actual page</p>"))
    page = await context.new_page()
    await page.goto("https://example.com/")
    local.put_session("run", "task", local.BrowserSession(
        playwright=pw, browser=browser, context=context, page=page,
        allowed_domains={"example.com"}, secrets={}, goal="Read page", history=[], steps=[],
        step_num=0, max_steps=2, challenge_type="", url=page.url,
        extra={"durable_session_id": "session", "owner_epoch": "epoch"}))
    ready, finish = asyncio.Event(), asyncio.Event()
    agent = WebAgent()
    async def decide(*args):
        ready.set()
        await finish.wait()
        return {"action": "done", "success": True, "result": "Read page"}
    agent._decide = decide
    async with browser_runtime_server():
        task = asyncio.create_task(agent.run("Read page", page.url, "user", run_id="run", task_id="task"))
        try:
            await asyncio.wait_for(ready.wait(), 10)
            assert (await repo.get_session("session")).runtime_session_id == endpoint
            async with httpx.AsyncClient() as client:
                denied = await client.post(endpoint + "/internal/browser/frame",
                    json={"session_id": "session", "actor_user_id": "user"})
                assert denied.status_code == 403
            frame = await other.capture_frame("user", "session")
            assert frame["url"] == page.url
            assert frame["frame_base64"]
            taken = await other.request_takeover("user", "session")
            assert service.get_session("session").controller_epoch == taken["controller_epoch"]
            await other.return_control("user", "session")
            finish.set()
            result = await asyncio.wait_for(task, 10)
            assert result["success"]
            assert not browser.is_connected()
        finally:
            finish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if browser.is_connected():
                await browser.close()
            await pw.stop()


@pytest.mark.asyncio
async def test_owner_lease_loss_closes_actual_browser(runtime, monkeypatch):
    import asyncio
    from backend.api.routers.browser_viewer import get_browser_service
    from playwright.async_api import async_playwright
    repo, owned, _, _ = runtime
    service = get_browser_service()
    monkeypatch.setattr(service, "_heartbeat_interval", 0.02)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    managed = await service.adopt_session(owned, playwright=pw, browser=browser, context=context, page=page)
    try:
        await repo.db.execute("UPDATE browser_sessions SET lease_expires_at=1 WHERE session_id='session'")
        await repo.db.commit()
        await asyncio.wait_for(managed._heartbeat, 5)
        assert not browser.is_connected()
        assert (await repo.get_session("session")).cleanup_state.value == "COMPLETE"
    finally:
        await service.detach_session("session")
        if browser.is_connected():
            await browser.close()
        await pw.stop()
