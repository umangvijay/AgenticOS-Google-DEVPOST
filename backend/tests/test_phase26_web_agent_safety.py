"""Dispatch regressions use real SQLite authority and a simulated browser transport.

These are not live Chromium, hosted-provider, or deployed-viewer evidence.
"""
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.models.browser_session import BrowserSession, ControllerState, ResourceState, CleanupState
from backend.repositories.browser_session_repository import BrowserSessionRepository, set_browser_session_repo
from backend.repositories.sqlite.database import DatabaseManager
from backend.services import browser_sessions as local
from backend.services.capacity_service import WORKER_ID
from backend.services.web_agent import WebAgent, WebAgentError, _DOCUMENT_ID_JS, _SNAPSHOT_JS


@pytest_asyncio.fixture
async def runtime(tmp_path):
    db = DatabaseManager(str(tmp_path / "browser.db"))
    await db.initialize()
    repo = BrowserSessionRepository(db)
    set_browser_session_repo(repo)
    owned = await repo.create_session(BrowserSession(
        "session", "user", "user", run_id="run", task_id="task",
        owner_worker_id=WORKER_ID, owner_epoch="epoch", owner_generation=1,
        lease_expires_at=int(time.time()) + 300, resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT, allowed_origins=["example.com"]))
    element = {"idx": 0, "tag": "a", "role": "link", "text": "Details", "href": "/details"}
    snapshot = {"url": "https://example.com/", "title": "Example", "text": "Details",
                "document_id": "doc-1", "elements": [element], "iframes": []}
    page = SimpleNamespace(url=snapshot["url"], document_id="doc-1", elements=[copy.deepcopy(element)],
                           content=AsyncMock(return_value="<p>Details</p>"))
    async def evaluate(script):
        if script == _DOCUMENT_ID_JS:
            return page.document_id
        assert script == _SNAPSHOT_JS
        return copy.deepcopy(page.elements)
    page.evaluate = evaluate
    yield repo, owned, snapshot, page
    local._SESSIONS.clear()
    set_browser_session_repo(None)
    await db.close()


async def validate(runtime, *, consequential=False, effect=False):
    repo, owned, snapshot, page = runtime
    agent = WebAgent()
    decision = {"action": "click", "index": 0}
    kind, args = agent._action_arguments(decision)
    await agent._validate_dispatch(page, snapshot, decision, {"example.com"}, repo, owned,
                                   kind, args, consequential=consequential, effect_recorded=effect)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["owner", "epoch", "lease", "controller", "closed", "missing",
                                       "document", "target", "host", "unavailable"])
async def test_dispatch_refuses_changed_authority_or_page(runtime, mutation, monkeypatch):
    repo, owned, snapshot, page = runtime
    if mutation in {"owner", "epoch", "lease", "controller", "closed"}:
        field, value = {"owner": ("owner_worker_id", "other"), "epoch": ("owner_epoch", "stale"),
                        "lease": ("lease_expires_at", 1), "controller": ("controller_state", "HUMAN"),
                        "closed": ("resource_state", "CLOSED")}[mutation]
        await repo.db.execute(f"UPDATE browser_sessions SET {field}=? WHERE session_id=?", (value, "session"))
        await repo.db.commit()
    elif mutation == "missing":
        await repo.db.execute("DELETE FROM browser_sessions")
        await repo.db.commit()
    elif mutation == "unavailable":
        monkeypatch.setattr(repo, "get_session", AsyncMock(side_effect=RuntimeError("store offline")))
    elif mutation == "document":
        page.document_id = "doc-2"
    elif mutation == "target":
        page.elements[0]["href"] = "/delete"
    elif mutation == "host":
        page.url = "https://evil.example/?next=example.com"
    with pytest.raises((WebAgentError, RuntimeError)):
        await validate(runtime)


@pytest.mark.asyncio
async def test_live_authority_accepts_read_and_requires_write_receipt(runtime):
    await validate(runtime)
    with pytest.raises(WebAgentError, match="EFFECT_NOT_RECORDED"):
        await validate(runtime, consequential=True)
    await validate(runtime, consequential=True, effect=True)


@pytest.mark.parametrize("decision", [
    {"action": "click", "index": 0}, {"action": "type", "index": 0, "text": "hello"},
    {"action": "select", "index": 0, "value": "red"}, {"action": "navigate", "url": "https://example.com"},
    {"action": "scroll", "direction": "down"}, {"action": "wait", "seconds": 1},
])
def test_model_protocol_maps_to_validated_action(decision):
    from backend.models.browser_observation import validate_action_arguments
    kind, args = WebAgent._action_arguments(decision)
    assert validate_action_arguments(kind, args) == (True, [])


@pytest.mark.parametrize("decision", [
    {"action": "click", "index": True}, {"action": "click", "index": -1},
    {"action": "click", "index": 0, "selector": "button"},
    {"action": "type", "index": 0, "text": "hello", "submit": "false"},
    {"action": "evaluate", "script": "alert(1)"},
])
def test_model_protocol_refuses_unsafe_fields(decision):
    with pytest.raises(WebAgentError):
        WebAgent._action_arguments(decision)


def register(runtime):
    repo, owned, snapshot, page = runtime
    session = local.BrowserSession(
        playwright=SimpleNamespace(stop=AsyncMock()), browser=SimpleNamespace(close=AsyncMock()),
        context=SimpleNamespace(close=AsyncMock()), page=page, allowed_domains={"example.com"},
        secrets={}, goal="Read details", history=[], steps=[], step_num=0, max_steps=2,
        challenge_type="", url=page.url, created_at=0,
        extra={"durable_session_id": owned.session_id, "owner_epoch": owned.owner_epoch})
    local.put_session("run", "task", session)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "validator", "approval", "after_preflight"])
async def test_run_does_not_dispatch_refused_action(runtime, monkeypatch, failure):
    repo, owned, snapshot, page = runtime
    register(runtime)
    agent = WebAgent()
    agent._snapshot = AsyncMock(return_value=snapshot)
    agent._execute = AsyncMock()
    if failure == "approval":
        snapshot["elements"][0]["submit"] = True
        page.elements[0]["submit"] = True
    agent._decide = AsyncMock(side_effect=[{"action": "click", "index": 0},
                                         {"action": "done", "result": "read", "success": True}])
    if failure == "validator":
        monkeypatch.setattr("backend.services.browser_action_validator.validate_browser_action",
                            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("validator unavailable")))
    elif failure == "after_preflight":
        original = agent._validate_dispatch
        calls = 0
        async def fence(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                await repo.set_controller("session", ControllerState.HUMAN)
            return await original(*args, **kwargs)
        agent._validate_dispatch = fence
    if failure == "after_preflight":
        from backend.services.browser_execution import StaleControllerError
        with pytest.raises(StaleControllerError):
            await agent.run("Read details", page.url, "user", run_id="run", task_id="task")
        assert agent._execute.await_count == 0
        assert (await repo.get_session("session")).cleanup_state is CleanupState.COMPLETE
        return
    result = await agent.run("Read details", page.url, "user", run_id="run", task_id="task")
    assert agent._execute.await_count == (0 if failure else 1)
    assert result["steps"][0]["executed"] is (failure is None)
    assert result["success"] is (failure is None)
    assert result["session_recovered"] is True
    durable = await repo.get_session("session")
    assert durable.cleanup_state is CleanupState.COMPLETE
    assert durable.resource_state is ResourceState.CLOSED
    if not failure:
        assert result["checkpoint"]


@pytest.mark.asyncio
async def test_failed_cleanup_retains_handles_and_retries(runtime):
    repo, _, _, _ = runtime
    session = register(runtime)
    session.context.close.side_effect = RuntimeError("browser hung")
    assert await local.sweep_expired_sessions() == 0
    assert local.get_session("run", "task") is session
    durable = await repo.get_session("session")
    assert durable.cleanup_state is CleanupState.FAILED
    assert durable.resource_state is ResourceState.READY
    session.context.close.side_effect = None
    assert await local.sweep_expired_sessions() == 1
    assert local.get_session("run", "task") is None
    assert (await repo.get_session("session")).resource_state is ResourceState.CLOSED


@pytest.mark.asyncio
async def test_old_local_runtime_cannot_close_new_owner(runtime):
    repo, _, _, _ = runtime
    register(runtime)
    await repo.db.execute("UPDATE browser_sessions SET owner_epoch='replacement'")
    await repo.db.commit()
    assert await local.sweep_expired_sessions() == 1
    durable = await repo.get_session("session")
    assert durable.cleanup_state is CleanupState.NOT_REQUIRED
    assert durable.resource_state is ResourceState.READY
