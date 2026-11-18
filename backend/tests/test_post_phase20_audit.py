"""Behavioral regressions from the post-Phase-20 source audit.

These tests use synthetic bytes and transport doubles; real-browser and
JetStream acceptance remain separate environment-dependent gates.
"""
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.artifact_verifier import verify_artifact
from backend.services.browser_execution import (
    BrowserExecutionError, BrowserExecutionService, ManagedBrowserSession,
)


async def _verify(manifest, files):
    record = SimpleNamespace(revision=1, content_checksum="record-digest",
                             file_manifest=manifest)
    repo = SimpleNamespace(get_for_owner=AsyncMock(return_value=record))
    return await verify_artifact(
        artifact_id="a", owner_user_id="owner", repo=repo,
        stored_bytes_fn=AsyncMock(return_value=files))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["sha256", "byte_length", "both"])
async def test_missing_file_integrity_never_passes(missing):
    entry = {"path": "result.txt", "sha256": hashlib.sha256(b"ok").hexdigest(),
             "byte_length": 2}
    for key in ("sha256", "byte_length"):
        if missing in (key, "both"):
            entry.pop(key)
    result = await _verify([entry], {"result.txt": b"ok"})
    assert result.verdict == "needs_review"
    assert not result.passed
    assert not any(c.get("check") == "all_manifest_files_verified"
                   for c in result.checks)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, -1, "2", 2.0, "not-a-number"])
async def test_invalid_manifest_length_fails_without_exception(bad):
    result = await _verify([{"path": "r", "sha256": hashlib.sha256(b"ok").hexdigest(),
                             "byte_length": bad}], {"r": b"ok"})
    assert result.verdict == "fail"


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", [
    [None], [{"path": "", "sha256": "a" * 64, "byte_length": 2}],
    [{"path": "r", "sha256": "not-a-sha", "byte_length": 2}],
])
async def test_malformed_manifest_is_a_typed_failure(manifest):
    assert (await _verify(manifest, {"r": b"ok"})).verdict == "fail"


@pytest.mark.asyncio
async def test_duplicate_paths_fail_and_empty_file_still_verifies():
    entry = {"path": "empty", "sha256": hashlib.sha256(b"").hexdigest(),
             "byte_length": 0}
    assert (await _verify([entry], {"empty": b""})).passed
    assert (await _verify([entry, entry], {"empty": b""})).verdict == "fail"


@pytest.mark.asyncio
async def test_nonbyte_storage_result_is_a_typed_failure():
    entry = {"path": "r", "sha256": hashlib.sha256(b"ok").hexdigest(),
             "byte_length": 2}
    assert (await _verify([entry], {"r": "ok"})).verdict == "fail"


def _session():
    return ManagedBrowserSession("s", "owner", "r", "t",
                                 ["https://allowed.example", "http://localhost:8123"],
                                 "worker", "epoch")


@pytest.mark.parametrize("url", [
    "http://localhost.evil.example/private", "http://127.0.0.1:8123/",
    "http://localhost:9999/", "https://allowed.example.evil/",
    "https://user:password@allowed.example/", "file:///tmp/synthetic",
    "https://allowed.example:bad/", "https://allowed.example\\@evil.example/",
])
def test_browser_requires_exact_valid_granted_origin(url):
    with pytest.raises(BrowserExecutionError):
        BrowserExecutionService()._assert_origin_allowed(_session(), url)


@pytest.mark.parametrize("url", ["https://ALLOWED.example:443/a", "http://localhost:8123/a"])
def test_browser_accepts_canonical_explicit_origin(url):
    BrowserExecutionService()._assert_origin_allowed(_session(), url)


@pytest.mark.asyncio
async def test_browser_request_guard_blocks_redirect_and_resource_destinations(monkeypatch):
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(
        return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]))
    service = BrowserExecutionService()
    for url, allowed in [("https://allowed.example/resource", True),
                          ("http://localhost.evil.example/redirect", False),
                          ("http://127.0.0.1:9000/internal", False)]:
        route = SimpleNamespace(request=SimpleNamespace(url=url),
                                continue_=AsyncMock(), abort=AsyncMock())
        await service._guard_request(_session(), route)
        if allowed:
            route.continue_.assert_awaited_once()
            route.abort.assert_not_awaited()
        else:
            route.abort.assert_awaited_once_with("blockedbyclient")
            route.continue_.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_shutdown_stops_playwright():
    session = _session()
    session._page = SimpleNamespace(close=AsyncMock())
    session._browser = SimpleNamespace(close=AsyncMock())
    session._playwright = SimpleNamespace(stop=AsyncMock())
    service = BrowserExecutionService()
    service._sessions[session.session_id] = session
    await service.shutdown()
    assert session.session_id not in service._sessions
    session._page.close.assert_awaited_once()
    session._browser.close.assert_awaited_once()
    session._playwright.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_nats_startup_timeout_closes_partial_client(monkeypatch):
    import asyncio
    from nats.aio.client import Client
    from backend.repositories.nats_message_bus import NatsMessageBus

    async def stalled_connect(self, **kwargs):
        await asyncio.Event().wait()

    close = AsyncMock()
    monkeypatch.setattr(Client, "connect", stalled_connect)
    monkeypatch.setattr(Client, "close", close)
    bus = NatsMessageBus(["nats://127.0.0.1:4222"], startup_timeout=0.02)
    assert not await asyncio.wait_for(bus.connect(), timeout=1)
    close.assert_awaited_once()
    assert not bus.connected()
    with pytest.raises(RuntimeError, match="not connected"):
        await bus.publish("workflow.events", {})


@pytest.mark.asyncio
async def test_rejection_http_body_survives_database_reload(tmp_path):
    import json
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    from backend.api.routers import experiments
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.experiment_repository import ExperimentRepository
    from backend.services.improvement_service import ImprovementService
    from backend.tests.test_phase20_evolution import _to_canary

    db = DatabaseManager(str(tmp_path / "rejection.db"))
    await db.initialize()
    try:
        repo = ExperimentRepository(db)
        exp_id = await _to_canary(ImprovementService(repo))
        app = FastAPI()
        app.state.factory = SimpleNamespace(experiment_repo=repo)
        app.include_router(experiments.router)
        user = AuthenticatedUser(user_id="reviewer", email="reviewer@example.com",
                                 name="Reviewer", role="admin", auth_provider="local")
        app.dependency_overrides[get_current_user] = lambda: user
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as client:
            response = await client.post(f"/experiments/{exp_id}/reject",
                                         json={"reason": "Verified regression"})
            assert response.status_code == 200, response.text
            assert response.json()["rejection_reason"] == "Verified regression"
        persisted = await repo.get_experiment(exp_id)
        assert persisted.rejection_reason == "Verified regression"
        assert persisted.approved_by == "user:reviewer"
        assert "candidate" in json.loads(persisted.results_json)
    finally:
        await db.close()
