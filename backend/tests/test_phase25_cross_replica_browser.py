"""Two API processes share a registry. Only the owning process runs Chromium."""
import multiprocessing
import socket
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest


class Form(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b'<input id="f" oninput="document.querySelector(\'p\').textContent=this.value"><p></p>')

    def log_message(self, *args):
        pass


def serve(worker, port, endpoint, registry, trusted):
    import os
    import uvicorn
    from fastapi import FastAPI
    from backend.api.routers import browser_viewer
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user, require_not_viewer
    from backend.services.browser_execution import BrowserExecutionService

    os.environ.update(BROWSER_SESSION_DB=registry, AGENTOS_BROWSER_TRUSTED_ENDPOINTS=trusted,
                      AGENTOS_BROWSER_INTERNAL_TOKEN="local-drill-token")
    svc = BrowserExecutionService(worker_id=worker, internal_endpoint=endpoint,
                                  allow_private_network=True)
    browser_viewer.set_browser_service(svc)
    app = FastAPI()
    app.include_router(browser_viewer.router, prefix="/api/v1")
    app.include_router(browser_viewer.internal_router, prefix="/api/v1")

    def owner():
        return AuthenticatedUser(user_id="owner-u", email="owner@example.test", name="Owner",
                                 role="user", auth_provider="local")
    app.dependency_overrides[get_current_user] = owner
    app.dependency_overrides[require_not_viewer] = owner

    @app.post("/test-agent/start")
    async def start(origin: str):
        session = await svc.open_session(owner_user_id="owner-u", run_id="run", task_id="task",
                                         allowed_origins=[origin])
        await svc.execute("owner-u", session.session_id, "goto", target=origin,
                          controller_epoch=session.controller_epoch)
        return {"session_id": session.session_id, "epoch": session.controller_epoch}

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")


@contextmanager
def replicas(tmp_path):
    with ExitStack() as stack:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Form)
        Thread(target=httpd.serve_forever, daemon=True).start()
        stack.callback(httpd.server_close)
        stack.callback(httpd.shutdown)
        ports = []
        for _ in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                ports.append(sock.getsockname()[1])
        urls = [f"http://127.0.0.1:{port}/api/v1" for port in ports]
        processes = []
        for worker, port, url in zip(("a", "b"), ports, urls):
            proc = multiprocessing.get_context("spawn").Process(
                target=serve, args=(worker, port, url, str(tmp_path/"registry.sqlite"), ",".join(urls)))
            proc.start()
            stack.callback(proc.join, 8)
            stack.callback(proc.terminate)
            processes.append(proc)
            deadline = time.monotonic() + 25
            while True:
                try:
                    if httpx.get(url+"/browser/sessions", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline or not proc.is_alive():
                    pytest.fail(f"browser worker {worker} failed startup")
                time.sleep(0.1)
        yield urls, processes, f"http://127.0.0.1:{httpd.server_port}"


def start_on_a(urls, origin):
    res = httpx.post(urls[0].replace("/api/v1", "/test-agent/start"),
                     params={"origin": origin}, timeout=30)
    res.raise_for_status()
    return res.json()


def test_two_worker_journey_takeover_return_fresh_observation(tmp_path):
    with replicas(tmp_path) as (urls, _, origin), httpx.Client(timeout=15) as client:
        session = start_on_a(urls, origin)
        sid = session["session_id"]
        base = urls[1]+"/browser/sessions"
        assert sid in [s["session_id"] for s in client.get(base).json()["sessions"]]
        path = base+"/"+sid
        assert client.get(path+"/frame").json()["screenshot_sha256"]
        assert client.post(path+"/pause").is_success
        assert client.post(path+"/resume").is_success
        epoch = client.post(path+"/takeover").json()["controller_epoch"]
        stale = {"action":"type", "target":"#f", "text":"stale", "controller_epoch":session["epoch"]}
        assert client.post(path+"/execute", json=stale).status_code == 409
        edited = client.post(path+"/execute", json={**stale, "text":"edited", "controller_epoch":epoch})
        assert edited.json()["status"] == "executed", edited.text
        returned = client.post(path+"/return-control").json()
        assert returned["requires_fresh_observation"]
        observed = client.post(urls[0]+"/internal/browser/execute",
            headers={"x-internal-token":"local-drill-token"},
            json={"session_id":sid, "actor_user_id":"owner-u", "action":"extract_text",
                  "controller_epoch":returned["controller_epoch"]})
        assert "edited" in observed.json()["detail"], observed.text
        assert client.delete(path).is_success
        assert sid not in [s["session_id"] for s in client.get(base).json()["sessions"]]


def test_worker_death_reports_honestly(tmp_path):
    with replicas(tmp_path) as (urls, processes, origin):
        sid = start_on_a(urls, origin)["session_id"]
        processes[0].terminate()
        processes[0].join(8)
        result = httpx.get(urls[1]+"/browser/sessions/"+sid+"/frame")
        assert result.status_code == 409 and "unreachable" in result.text
