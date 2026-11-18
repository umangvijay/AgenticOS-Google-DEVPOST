"""Phase 20 §20.16: viewer/takeover ownership — SERVER-ENFORCED.
A stranger cannot execute, view, or take over another user's session;
takeover rotates the controller epoch and is audited; the router maps
ownership violations to 403 and unknown sessions to 409/404. Real
Playwright sessions back the journey."""
import http.server
import threading

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from backend.tests.test_phase20_browser_service import _App

from backend.services.browser_execution import (
    BrowserExecutionService, NotControllerError)

pytestmark = pytest.mark.asyncio

@pytest.fixture(scope="module")
def app_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest_asyncio.fixture
async def service():
    svc = BrowserExecutionService(headless=True, allow_private_network=True)
    yield svc
    await svc.shutdown()


async def _owned_session(service, app_url):
    return await service.open_session(
        owner_user_id="owner-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])


async def test_stranger_cannot_execute_or_close(service, app_url):
    session = await _owned_session(service, app_url)
    with pytest.raises(NotControllerError):
        await service.execute("stranger-9", session.session_id, "goto",
                              target=app_url)
    with pytest.raises(NotControllerError):
        await service.close_session("stranger-9", session.session_id)
    # The stranger cannot see the session in their list either.
    assert await service.list_sessions("stranger-9") == []


async def test_takeover_rotates_epoch_and_is_audited(service, app_url):
    session = await _owned_session(service, app_url)
    old_epoch = session.controller_epoch
    result = await service.request_takeover("owner-1", session.session_id)
    assert result["controller_epoch"] != old_epoch, \
        "takeover invalidates the previous control epoch"
    assert result["controller"] == "user:owner-1"
    takeovers = [h for h in session.history if h.action == "takeover"]
    assert len(takeovers) == 1 and takeovers[0].executed
    assert "epoch" in takeovers[0].detail


async def test_viewing_is_read_only_membership(service, app_url):
    session = await _owned_session(service, app_url)
    view = await service.begin_viewing("owner-1", session.session_id)
    assert view["mode"] == "viewing"
    # Viewing does NOT grant control to a second user (only the owner
    # can view their own session in this model).
    with pytest.raises(NotControllerError):
        await service.begin_viewing("stranger-9", session.session_id)
    listing = await service.list_sessions("owner-1")
    assert listing and listing[0]["session_id"] == session.session_id


# ── the HTTP surface ───────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(service):
    from fastapi import FastAPI
    from backend.api.routers import browser_viewer
    from backend.api.dependencies import auth as auth_deps

    browser_viewer.set_browser_service(service)
    app = FastAPI()
    app.include_router(browser_viewer.router)

    def actor(user_id: str, role: str = "user"):
        async def _override():
            return auth_deps.AuthenticatedUser(
                user_id=user_id, email=f"{user_id}@example.com",
                name=user_id, role=role, auth_provider="local")
        return _override

    # Only identity is overridden; the REAL require_not_viewer guard runs.
    app.dependency_overrides[auth_deps.get_current_user] = actor("owner-1")
    yield {"client": AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://test"),
           "actor": actor, "app": app}


async def test_router_takeover_view_execute_flow(client, service, app_url):
    session = await _owned_session(service, app_url)
    c = client["client"]
    # Owner opens view + executes a goto through the API.
    view = await c.post(f"/browser/sessions/{session.session_id}/view")
    assert view.status_code == 200 and view.json()["mode"] == "viewing"
    state = await c.get(f"/browser/sessions/{session.session_id}")
    body = state.json()
    assert body["role"] in ("controller", "viewer")
    handoff = await c.post(f"/browser/sessions/{session.session_id}/takeover")
    assert handoff.status_code == 200
    exec_res = await c.post(
        f"/browser/sessions/{session.session_id}/execute",
        json={"action": "goto", "target": app_url,
              "controller_epoch": handoff.json()["controller_epoch"]})
    assert exec_res.status_code == 200
    payload = exec_res.json()
    assert payload["executed"] is True and payload["status"] == "executed"
    assert payload["url_after"].startswith("http://127.0.0.1")


async def test_router_refuses_non_owner_with_403(client, service, app_url):
    session = await _owned_session(service, app_url)
    c = client["client"]
    actor = client["actor"]

    from backend.api.routers import browser_viewer
    from backend.api.dependencies import auth as auth_deps

    # Swap the override to a STRANGER.
    async def stranger():
        return auth_deps.AuthenticatedUser(
            user_id="stranger-9", email="s@example.com", name="s",
            role="user", auth_provider="local")
    c2 = AsyncClient(transport=ASGITransport(app=client["client"]._transport.app),
                     base_url="http://test")
    browser_viewer.router  # noqa
    app = c2._transport.app
    app.dependency_overrides[auth_deps.get_current_user] = stranger
    try:
        takeover = await c2.post(
            f"/browser/sessions/{session.session_id}/takeover")
        assert takeover.status_code == 403, takeover.text
        execute = await c2.post(
            f"/browser/sessions/{session.session_id}/execute",
            json={"action": "goto", "target": app_url,
                  "controller_epoch": session.controller_epoch})
        assert execute.status_code == 403
        close = await c2.delete(
            f"/browser/sessions/{session.session_id}")
        assert close.status_code == 403
    finally:
        await c2.aclose()


async def test_router_viewer_role_cannot_act(client, service, app_url):
    """A viewer-role account never gets write authority — viewer
    accounts are refused by require_not_viewer before ownership checks."""
    session = await _owned_session(service, app_url)
    c = client["client"]
    actor = client["actor"]
    from backend.api.routers import browser_viewer
    from backend.api.dependencies import auth as auth_deps

    async def viewer_user():
        return auth_deps.AuthenticatedUser(
            user_id="owner-1", email="o@example.com", name="o",
            role="viewer", auth_provider="local")

    app = c._transport.app
    # Override ONLY the identity dependency: the REAL require_not_viewer
    # then runs against a viewer identity and must produce the 403.
    app.dependency_overrides[auth_deps.get_current_user] = viewer_user
    try:
        res = await c.post(
            f"/browser/sessions/{session.session_id}/execute",
            json={"action": "goto", "target": app_url})
        assert res.status_code == 403, res.text
    finally:
        app.dependency_overrides[auth_deps.get_current_user] = \
            actor("owner-1")
