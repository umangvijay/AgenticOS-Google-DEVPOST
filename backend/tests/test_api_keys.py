"""
AgentOS — Per-user API key tests

Keys authenticate identically to JWTs (same role/scoping), the raw key
is returned exactly once, revocation takes effect immediately, and
disabled users lose access even with a live key.
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from backend.api.dependencies.auth import _authenticate_api_key
from backend.api.dependencies.auth import AuthenticatedUser
from backend.repositories.sqlite.api_keys_repository import SQLiteApiKeysRepository
from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def keys_repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "keys.db"))
    await db.initialize()
    # api_keys.user_id has a real FK to users — create the test users.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for uid in ("user-1", "user-2"):
        await db.execute(
            "INSERT INTO users (id, email, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{uid}@x", uid, now, now),
        )
    await db.commit()
    yield SQLiteApiKeysRepository(db)
    await db.close()


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.mark.asyncio
async def test_key_lifecycle_create_resolve_revoke(keys_repo):
    raw = "vry_" + "a" * 32
    key_id = await keys_repo.create_key("user-1", _hash(raw), raw[:8], "ci")

    resolved = await keys_repo.resolve_key(_hash(raw))
    assert resolved == {"user_id": "user-1", "key_id": key_id}

    # last_used_at gets touched on every successful resolve
    rows = await keys_repo.list_keys("user-1")
    assert rows[0]["last_used_at"] is not None
    assert all("key_hash" not in r for r in rows), "hash must never be listed"

    assert await keys_repo.revoke_key("user-1", key_id) is True
    assert await keys_repo.resolve_key(_hash(raw)) is None
    # revoking again is a 404, not a double-revoke
    assert await keys_repo.revoke_key("user-1", key_id) is False


@pytest.mark.asyncio
async def test_keys_are_user_scoped(keys_repo):
    raw = "vry_" + "b" * 32
    key_id = await keys_repo.create_key("user-1", _hash(raw), raw[:8], "x")
    assert await keys_repo.revoke_key("user-2", key_id) is False
    assert await keys_repo.resolve_key(_hash(raw)) is not None


@pytest.mark.asyncio
async def test_authenticate_via_api_key_happy_and_sad_paths():
    raw = "vry_live_key"
    keys_repo = SimpleNamespace(
        resolve_key=AsyncMock(side_effect=lambda h: {"user_id": "u1", "key_id": "k1"} if h == _hash(raw) else None),
    )
    user_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value={"id": "u1", "email": "u1@x", "name": "U", "role": "user", "is_active": True}),
    )
    factory = SimpleNamespace(api_keys_repo=keys_repo, user_repo=user_repo)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=factory)))

    user = await _authenticate_api_key(request, raw)
    assert isinstance(user, AuthenticatedUser)
    assert user.user_id == "u1" and user.auth_provider == "api_key"

    # Revoked/unknown key → 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await _authenticate_api_key(request, "vry_revoked")
    assert e.value.status_code == 401

    # Disabled account → 401 even with a live key
    user_repo.get_by_id = AsyncMock(return_value={"id": "u1", "is_active": False})
    with pytest.raises(HTTPException) as e:
        await _authenticate_api_key(request, raw)
    assert e.value.status_code == 401
