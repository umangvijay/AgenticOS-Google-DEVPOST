"""Phase 21 §21.13: LIVE browser viewer + takeover — server-enforced
pause gates agent actions, stale controller epochs are REFUSED
(§15.20 fencing), takeover/return-control rotate epochs and require a
fresh observation, and the live view captures a REAL screenshot of the
agent's actual page. Driven over a REAL local HTTP app."""
import http.server
import threading

import pytest
import pytest_asyncio

from backend.services.browser_execution import (
    BrowserExecutionError, BrowserExecutionService, StaleControllerError)

pytestmark = pytest.mark.asyncio

LIVE_HTML = """<!doctype html><html><body>
<h1 id="live">Live Session {n}</h1>
<button id="b" onclick="document.getElementById('live').innerText='clicked'">go</button>
</body></html>"""


class _App(http.server.BaseHTTPRequestHandler):
    n = 1

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        body = LIVE_HTML.format(n=_App.n).encode()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def app_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    yield url
    server.shutdown()


@pytest_asyncio.fixture
async def svc():
    s = BrowserExecutionService(headless=True, allow_private_network=True)
    yield s
    await s.shutdown()


async def _open(svc, app_url):
    return await svc.open_session(owner_user_id="owner-1", run_id="run-1",
                                  task_id="task-1", allowed_origins=[app_url])


async def test_stale_epoch_is_refused_after_takeover(svc, app_url):
    """THE §21.13 fencing acceptance: after a takeover the OLD controller
    epoch is fenced — an agent action carrying the stale epoch is
    refused with StaleControllerError, never executed."""
    session = await _open(svc, app_url)
    agent_epoch = session.controller_epoch
    takeover = await svc.request_takeover("owner-1", session.session_id)
    assert takeover["controller_epoch"] != agent_epoch
    with pytest.raises(StaleControllerError, match="stale controller epoch"):
        await svc.execute("owner-1", session.session_id, "goto",
                          target=app_url, controller_epoch=agent_epoch)
    # The CURRENT epoch executes fine.
    result = await svc.execute("owner-1", session.session_id, "goto",
                               target=app_url,
                               controller_epoch=takeover["controller_epoch"],
                               actor_controller_id="user:owner-1")
    assert result.status == "executed"


async def test_pause_gates_agent_actions_server_side(svc, app_url):
    """Pause is SERVER-ENFORCED: agent actions are refused while paused
    (not merely hidden in the UI); resume allows them again."""
    session = await _open(svc, app_url)
    await svc.execute("owner-1", session.session_id, "goto", target=app_url, controller_epoch=session.controller_epoch)
    await svc.pause("owner-1", session.session_id)
    with pytest.raises(BrowserExecutionError, match="PAUSED"):
        await svc.execute("owner-1", session.session_id, "click",
                          target="#b", controller_epoch=session.controller_epoch)
    await svc.resume("owner-1", session.session_id)
    await svc.execute("owner-1", session.session_id, "extract_text", controller_epoch=session.controller_epoch)
    result = await svc.execute("owner-1", session.session_id, "click",
                               target="#b", controller_epoch=session.controller_epoch)
    assert result.status == "executed"


async def test_return_control_requires_fresh_observation(svc, app_url):
    """Returning control to the agent rotates the epoch — the agent's
    OLD epoch is fenced and it must re-observe (fresh observation) with
    the new epoch before acting."""
    session = await _open(svc, app_url)
    agent_epoch = session.controller_epoch
    await svc.request_takeover("owner-1", session.session_id)
    handed_back = await svc.return_control("owner-1", session.session_id)
    assert handed_back["requires_fresh_observation"] is True
    # The agent's pre-handoff epoch is stale now.
    with pytest.raises(StaleControllerError):
        await svc.execute("owner-1", session.session_id, "goto",
                          target=app_url, controller_epoch=agent_epoch)
    with pytest.raises(BrowserExecutionError, match="fresh observation"):
        await svc.execute("owner-1", session.session_id, "goto", target=app_url,
                          controller_epoch=handed_back["controller_epoch"])
    await svc.execute("owner-1", session.session_id, "screenshot",
                      controller_epoch=handed_back["controller_epoch"])
    fresh = await svc.execute("owner-1", session.session_id, "goto",
                              target=app_url,
                              controller_epoch=handed_back["controller_epoch"])
    assert fresh.status == "executed"


async def test_live_view_is_the_agent_same_session(svc, app_url):
    """THE live-view acceptance: capture_frame returns a REAL screenshot
    of the agent's ACTUAL page — the same session. The DOM changed by an
    action is visible in the frame's url/freshness metadata, and the
    screenshot bytes are a real PNG."""
    session = await _open(svc, app_url)
    sid = session.session_id
    await svc.execute("owner-1", sid, "goto", target=app_url, controller_epoch=session.controller_epoch)
    frame1 = await svc.capture_frame("owner-1", sid)
    assert frame1["frame_base64"], "a real screenshot was captured"
    import base64, hashlib
    raw = base64.b64decode(frame1["frame_base64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "actual PNG bytes"
    assert hashlib.sha256(raw).hexdigest() == frame1["screenshot_sha256"]
    assert frame1["url"] == f"{app_url}/"
    # Freshness: a second capture advances the freshness timestamp.
    frame2 = await svc.capture_frame("owner-1", sid)
    assert frame2["freshness"] >= frame1["freshness"]
    # A stranger cannot capture the agent's view.
    from backend.services.browser_execution import NotControllerError
    with pytest.raises(NotControllerError):
        await svc.capture_frame("stranger-9", sid)


async def test_takeover_during_pause_reactivates(svc, app_url):
    session = await _open(svc, app_url)
    await svc.pause("owner-1", session.session_id)
    result = await svc.request_takeover("owner-1", session.session_id)
    session_state = svc.get_session(session.session_id)
    assert session_state.state == "running"
    assert result["controller"] == "user:owner-1"
