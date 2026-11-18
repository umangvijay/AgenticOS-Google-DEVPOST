"""Exercise the memory HTTP and cleanup paths on a separately restored database."""
import asyncio
from contextlib import asynccontextmanager
import shutil
import uuid

import asyncpg
from fastapi import FastAPI
import httpx
import pytest

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.api.routers.memory import router
from backend.repositories.postgres.database import PostgresDatabaseManager
from backend.repositories.postgres.repositories import PostgresMemoryRepository
from backend.tests.test_phase24_feedback_lifecycle_pg import _pg_up
from backend.workers.memory_cleanup_worker import run_cleanup_pass

DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"


async def database_tool(*args, input_data=None):
    process = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", "agentos-postgres-1", *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(input_data), timeout=60)
    assert process.returncode == 0, stderr.decode()[-1000:]
    return stdout


@asynccontextmanager
async def memory_api(repo, identity):
    from types import SimpleNamespace
    app = FastAPI()
    app.state.factory = SimpleNamespace(memory_repo=repo)
    async def current_user():
        owner = identity["user"]
        return AuthenticatedUser(owner, owner + "@example.test", owner, "user", "local")
    app.dependency_overrides[get_current_user] = current_user
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        yield client


@pytest.mark.asyncio
async def test_restored_database_preserves_memory_ownership_deletion_and_cleanup(monkeypatch):
    if not _pg_up() or not shutil.which("docker"):
        pytest.skip("Requires the local PostgreSQL Compose service and Docker CLI")
    # Provider inference and login are independent release gates.
    async def unavailable_embedding(*args, **kwargs):
        return None
    monkeypatch.setattr("backend.api.routers.memory.embed_text_metered", unavailable_embedding)
    schema = "p26_mem_" + uuid.uuid4().hex
    target = "p26_restore_" + uuid.uuid4().hex
    source = PostgresDatabaseManager(DSN, schema=schema)
    restored = PostgresDatabaseManager(DSN.rsplit("/", 1)[0] + "/" + target, schema=schema)
    admin = await asyncpg.connect(DSN)
    created = False
    identity = {"user": "owner"}

    async def add(client, content, memory_type="semantic"):
        result = await client.post("/memory", json={"content": content, "memory_type": memory_type,
                                   "metadata": {"memory_class": "factual"}})
        assert result.status_code == 201, result.text
        return result.json()["memory_id"]

    try:
        await source.initialize()
        repo = PostgresMemoryRepository(source)
        async with memory_api(repo, identity) as client:
            kept = await add(client, "Preferred layout is compact.", "profile")
            removed = await add(client, "Old schedule starts at noon.")
            summary = await add(client, "Cached summary of the old schedule.")
            identity["user"] = "peer"
            peer = await add(client, "Peer keeps a separate schedule.")
            assert (await client.delete("/memory/" + removed)).status_code == 404
            identity["user"] = "owner"
            await repo.register_dependency(removed, derived_kind="cached_summary", derived_id=summary)
            assert (await client.delete("/memory/" + removed)).status_code == 200
            assert (await client.delete("/memory/" + removed)).status_code == 404
            with pytest.raises(ValueError, match="deleted memory"):
                await repo.register_dependency(removed, derived_kind="cached_summary", derived_id=peer)

        # Preserve the on-disk representation used by pre-0022 installations.
        await source.execute("""INSERT INTO pg_memories(id,user_id,content,memory_class)
            VALUES ('old-root','legacy','old source','profile'),('old-child','legacy','old summary','semantic')""")
        await repo.register_dependency("old-root", derived_kind="cached_summary", derived_id="old-child")
        await source.execute("UPDATE pg_memories SET deletion_state='deleted' WHERE id='old-root'")
        await source.execute("INSERT INTO pg_memory_cleanup_jobs(memory_id) VALUES ('old-root')")

        dump = await database_tool("pg_dump", "-U", "agentos", "-d", "agentos_test", "-n", schema,
                                   "--no-owner", "--no-privileges")
        await admin.execute(f'CREATE DATABASE "{target}"')
        created = True
        await database_tool("psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "agentos", "-d", target,
                            input_data=dump)
        await restored.initialize()
        recovered = PostgresMemoryRepository(restored)
        async with memory_api(recovered, identity) as client:
            listed = (await client.get("/memory", params={"memory_type": "profile"})).json()
            assert [(m["id"], m["memory_type"], m["memory_class"]) for m in listed["memories"]] == [
                (kept, "profile", "factual")]
            assert await recovered.get_memory("owner", removed) is None
            assert (await recovered.list_memories("legacy", memory_type="semantic"))[0]["id"] == "old-child"
            job = await recovered.claim_cleanup_job("first-worker")
            assert job["memory_id"] == removed and job["dependents"] == [{"kind": "cached_summary", "id": summary}]
            assert not await recovered.record_cleanup_progress(job["job_id"], worker_id="foreign", state="complete")
            assert await recovered.record_cleanup_progress(job["job_id"], worker_id="first-worker", state="failed")
            assert await run_cleanup_pass(recovered) >= 1
            rows = (await client.get("/memory")).json()["memories"]
            assert [r["id"] for r in rows] == [kept]
            assert await recovered.get_memory("owner", summary) is None
            assert await recovered.get_memory("legacy", "old-child") is None
            assert await recovered.get_memory("peer", peer) is not None
            assert await add(client, "Written after restoration.")
            ids = await asyncio.gather(*(recovered.store_memory("owner", "Repeated delivery.",
                kind="semantic", metadata={"source_event_id": "replayed-event"}) for _ in range(8)))
            assert len(set(ids)) == 1
            assert await recovered.store_memory("peer", "Peer delivery.", "semantic",
                {"source_event_id": "replayed-event"}) != ids[0]
            assert (await restored.fetch_one("SELECT state FROM pg_memory_cleanup_jobs WHERE id=$1",
                                             job["job_id"]))["state"] == "complete"
    finally:
        await restored.close()
        if created:
            await admin.execute(f'DROP DATABASE "{target}"')
        await admin.close()
        await source.close(drop_schema=True)
