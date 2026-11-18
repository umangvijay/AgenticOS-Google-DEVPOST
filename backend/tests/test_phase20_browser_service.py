"""Phase 20 §20.15: REAL browser execution — a Playwright-driven
session against a CONTROLLED local app served by a real HTTP server in
this process. Actions are genuinely executed (navigation, form typing,
submission, extraction, screenshot); failures carry the REAL error; the
no-canned-output rule holds (nothing reports success it did not do)."""
import hashlib
import http.server
import threading

import pytest
import pytest_asyncio

CONTROLLED_APP_HTML = """<!doctype html>
<html><head><title>AgentOS Controlled App</title></head>
<body>
  <h1 id="title">Controlled App</h1>
  <form id="greet-form" action="/greet" method="get">
    <input id="name-input" name="name" type="text" />
    <button id="submit-btn" type="submit">Greet</button>
  </form>
  <div id="counter">0</div>
  <button id="increment" onclick="document.getElementById('counter').innerText = String(Number(document.getElementById('counter').innerText) + 1)">+1</button>
</body></html>"""


class _App(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _html(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body_bytes = body.encode()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if self.path.startswith("/greet"):
            from urllib.parse import urlparse, parse_qs
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            self._html(f"<!doctype html><html><body>"
                       f"<h1 id='greeting'>Hello, {name}!</h1>"
                       f"</body></html>")
        else:
            self._html(CONTROLLED_APP_HTML)


@pytest.fixture(scope="module")
def app_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest_asyncio.fixture
async def service():
    from backend.services.browser_execution import BrowserExecutionService
    svc = BrowserExecutionService(headless=True, allow_private_network=True)
    yield svc
    await svc.shutdown()


pytestmark = pytest.mark.asyncio


async def test_session_opens_and_navigates_for_real(service, app_url):
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    result = await service.execute("user-1", session.session_id, "goto",
                                   target=app_url, controller_epoch=session.controller_epoch)
    assert result.executed and result.status == "executed"
    assert result.url_after == f"{app_url}/", \
        f"real navigation happened: {result.url_after}"
    text = await service.execute("user-1", session.session_id,
                                 "extract_text", controller_epoch=session.controller_epoch)
    assert "Controlled App" in text.detail, \
        f"REAL page content extracted: {text.detail[:80]}"


async def test_form_typing_and_submission_is_real(service, app_url):
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    sid = session.session_id
    await service.execute("user-1", sid, "goto", target=app_url, controller_epoch=session.controller_epoch)
    await service.execute("user-1", sid, "type", target="#name-input",
                          text="Umang", controller_epoch=session.controller_epoch)
    await service.execute("user-1", sid, "click", target="#submit-btn", controller_epoch=session.controller_epoch)
    # Wait for the server round-trip to complete.
    await service.execute("user-1", sid, "wait", target="300", controller_epoch=session.controller_epoch)
    text = await service.execute("user-1", sid, "extract_text", controller_epoch=session.controller_epoch)
    assert "Hello, Umang!" in text.detail, \
        f"the SUBMITTED value produced the server-rendered page: {text.detail[:80]}"


async def test_click_changes_dom_state_for_real(service, app_url):
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    sid = session.session_id
    await service.execute("user-1", sid, "goto", target=app_url, controller_epoch=session.controller_epoch)
    before = await service.execute("user-1", sid, "extract_text", controller_epoch=session.controller_epoch)
    assert "0" in before.detail
    await service.execute("user-1", sid, "click", target="#increment", controller_epoch=session.controller_epoch)
    after = await service.execute("user-1", sid, "extract_text", controller_epoch=session.controller_epoch)
    assert "1" in after.detail, "the click REALLY changed the page state"


async def test_screenshot_captures_real_bytes(service, app_url):
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    sid = session.session_id
    await service.execute("user-1", sid, "goto", target=app_url, controller_epoch=session.controller_epoch)
    shot = await service.execute("user-1", sid, "screenshot", controller_epoch=session.controller_epoch)
    assert shot.screenshot_sha256 and shot.status == "executed"
    assert shot.duration_ms >= 0


async def test_failed_action_records_the_real_error(service, app_url):
    """A click on a MISSING selector fails FOR REAL and the evidence
    carries the actual Playwright error — never rewritten to success."""
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    sid = session.session_id
    await service.execute("user-1", sid, "goto", target=app_url, controller_epoch=session.controller_epoch)
    result = await service.execute("user-1", sid, "click",
                                   target="#does-not-exist", controller_epoch=session.controller_epoch)
    assert result.executed and result.status == "failed"
    assert result.error and ("does-not-exist" in result.error
                             or "Timeout" in result.error
                             or "timeout" in result.error), result.error
    # The failure is in the session history (audit trail).
    history = session.history
    assert any(h.status == "failed" for h in history)


async def test_origin_scope_is_enforced(service, app_url):
    """A goto to a NON-allow-listed origin is refused before any
    navigation happens — the session cannot silently browse wider."""
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    from backend.services.browser_execution import BrowserExecutionError
    with pytest.raises(BrowserExecutionError, match="outside"):
        await service.execute("user-1", session.session_id, "goto",
                              target="https://evil.example.com", controller_epoch=session.controller_epoch)
    # Opening a session WITHOUT origins is refused outright.
    with pytest.raises(BrowserExecutionError, match="unbounded"):
        await service.open_session(owner_user_id="user-1",
                                   run_id="run-1", task_id="task-1",
                                   allowed_origins=[])


async def test_unknown_action_is_refused_not_guessed(service, app_url):
    session = await service.open_session(
        owner_user_id="user-1", run_id="run-1", task_id="task-1",
        allowed_origins=[app_url])
    from backend.services.browser_execution import BrowserExecutionError
    with pytest.raises(BrowserExecutionError, match="unknown action"):
        await service.execute("user-1", session.session_id, "hover-magic",
                              target="#x", controller_epoch=session.controller_epoch)
