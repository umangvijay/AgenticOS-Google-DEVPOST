"""Unfiltered memory search spans authorized classes; explicit filters narrow it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_memory_search_optional_type_preserves_classes_and_owner(tmp_path, monkeypatch, method):
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
    from backend.api.routers import memory
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.memory_repository import SQLiteMemoryRepository
    db = DatabaseManager(str(tmp_path / "memory.db"))
    await db.initialize()
    app = FastAPI()
    app.state.factory = SimpleNamespace(memory_repo=SQLiteMemoryRepository(db))
    actor_id = ["owner"]
    async def actor():
        return AuthenticatedUser(actor_id[0], "fixture@example.test", "Fixture", "user", "local")
    app.dependency_overrides[get_current_user] = actor
    app.include_router(memory.router)
    # Exercise real persisted lexical retrieval with embeddings unavailable.
    monkeypatch.setattr(memory, "embed_text_metered", AsyncMock(return_value=None))
    expected = {}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for kind in ("semantic", "profile", "workflow"):
                response = await client.post("/memory", json={
                    "content": f"observatory {kind} memory", "memory_type": kind,
                    "metadata": {"explicit": True, "source_event_id": kind}})
                assert response.status_code == 201, response.text
                expected[kind] = response.json()["memory_id"]
            actor_id[0] = "other-owner"
            other = await client.post("/memory", json={"content": "observatory private memory"})
            assert other.status_code == 201
            actor_id[0] = "owner"

            async def search(kind=None):
                payload = {"query": "observatory", "limit": 10}
                if kind is not None:
                    payload["memory_type"] = kind
                if method == "GET":
                    payload["q"] = payload.pop("query")
                    return await client.get("/memory/search", params=payload)
                return await client.post("/memory/search", json=payload)

            response = await search()
            assert response.status_code == 200, response.text
            assert {r["memory_id"] for r in response.json()["results"]} == set(expected.values())
            assert not response.json()["abstained"]
            for kind, memory_id in expected.items():
                filtered = await search(kind)
                assert filtered.status_code == 200, filtered.text
                assert {r["memory_id"] for r in filtered.json()["results"]} == {memory_id}
            assert (await search("unknown")).status_code == 422
    finally:
        await db.close()
