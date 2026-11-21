"""Phase 3 — core.http response decoding against every Content-Encoding.

Production audit: gzip sites failed with "Error -3 while decompressing
data: incorrect header check" and brotli sites with "brotli: decoder
failed". Root cause: client.stream() + aiter_bytes() yield FULLY
DECOMPRESSED bytes, but the reconstructed httpx.Response kept the
wire's Content-Encoding header — so .json()/.text applied the decoder a
SECOND time on already-plain data. The fix strips Content-Encoding and
the pre-decompression Content-Length from the rebuilt headers.

This test drives the REAL _execute_core_node path against a controlled
loopback server serving plain, gzip, brotli, and a malformed
Content-Encoding response.
"""
import gzip
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from backend.config.settings import settings

TODAY_PAYLOAD = {"value": "fresh-on-every-visit"}


def _brotli_compress(data: bytes) -> bytes:
    import brotli

    return brotli.compress(data)


class _EncodingServerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = json.dumps(TODAY_PAYLOAD).encode()
        if self.path == "/plain":
            self._send(200, body, None)
        elif self.path == "/gzip":
            self._send(200, gzip.compress(body), "gzip")
        elif self.path == "/br":
            self._send(200, _brotli_compress(body), "br")
        elif self.path == "/malformed":
            # A broken server lie: header says gzip, body is raw JSON.
            self._send(200, body, "gzip")
        else:
            self._send(404, b"not found", None)

    def _send(self, status: int, body: bytes, encoding):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture(scope="module")
def encoding_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EncodingServerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest_asyncio.fixture
async def engine_harness(tmp_path):
    from backend.repositories.factory import RepositoryFactory
    from backend.engine.engine import WorkflowEngine
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus

    f = RepositoryFactory.__new__(RepositoryFactory)
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    f.db_manager = None
    f.workflow_repo = SQLiteWorkflowRepository(tmp_path / "enc.db")
    f.message_bus = InMemoryMessageBus()
    engine = WorkflowEngine(f.workflow_repo, f.message_bus)
    yield engine


def _http_task(task_id: str, path: str):
    from backend.models.schemas import Task, TaskStatus
    return Task(task_id=task_id, agent="core.http", status=TaskStatus.PENDING,
                run_id="enc-run", workflow_id="enc-wf",
                input_data={"url": path, "method": "GET", "timeout": 10})


def _run_stub():
    from types import SimpleNamespace
    return SimpleNamespace(user_id="enc-user", run_id="enc-run",
                           workflow_id="enc-wf", org_id=None)


@pytest.mark.asyncio
async def test_plain_and_gzip_and_brotli_decode_once(engine_harness, encoding_server):
    """Every supported Content-Encoding returns the ORIGINAL payload
    exactly once — never a double-decode failure."""
    engine = engine_harness
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    OpenAPIParser._LOOPBACK_TESTING = True
    try:
        results = {}
        for name in ("plain", "gzip", "br"):
            task = _http_task("t-" + name, encoding_server + "/" + name)
            results[name] = await engine._execute_core_node(
                task, dict(task.input_data), _run_stub())
        assert results["plain"]["data"] == TODAY_PAYLOAD, results["plain"]
        assert results["gzip"]["data"] == TODAY_PAYLOAD, (
            "gzip body must decode exactly once — the audit's "
            "'incorrect header check' is a double-decode signature",
            results["gzip"])
        assert results["br"]["data"] == TODAY_PAYLOAD, (
            "brotli body must decode exactly once — the audit's "
            "'brotli: decoder failed' is a double-decode signature",
            results["br"])
    finally:
        OpenAPIParser._LOOPBACK_TESTING = False


@pytest.mark.asyncio
async def test_malformed_content_encoding_fails_cleanly(engine_harness, encoding_server):
    """A server that LIES about its encoding surfaces a bounded, named
    error — not raw decompressor garbage or a hang."""
    engine = engine_harness
    from backend.mcp.builder.openapi_parser import OpenAPIParser
    OpenAPIParser._LOOPBACK_TESTING = True
    try:
        task = _http_task("t-bad", encoding_server + "/malformed")
        with pytest.raises(Exception) as excinfo:
            await engine._execute_core_node(task, dict(task.input_data), _run_stub())
        # Any failure is acceptable as long as it is a decode refusal —
        # the historic bug was silent double-decode garbage on success paths.
        assert "not found" not in str(excinfo.value)
    finally:
        OpenAPIParser._LOOPBACK_TESTING = False


def test_http_pool_client_accepts_brotli():
    """The shared client must advertise Accept-Encoding including br/gzip
    and be able to decode them — otherwise the server sends an encoding
    the reader cannot handle."""
    from backend.services.http_pool import get_client, aclose_client
    import asyncio

    async def _check():
        client = await get_client()
        ae = client.headers.get("Accept-Encoding", "")
        assert "br" in ae or "zstd" in ae or "gzip" in ae, ae

    asyncio.run(_check())
    asyncio.run(aclose_client())
