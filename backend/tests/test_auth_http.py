"""HTTP authentication against real SQLite repositories and signed RS256 tokens."""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_disabled_account_and_demotion_take_effect_over_http(tmp_path, monkeypatch):
    from backend.api.routers import auth as routes, blogs
    from backend.api.dependencies import auth as dependencies
    from backend.config.settings import settings
    from backend.repositories.factory import RepositoryFactory
    from backend.security.jwt_manager import JWTManager

    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY", None)
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "JWT_PRIVATE_KEY_PATH", str(tmp_path / "private.pem"))
    monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(tmp_path / "public.pem"))
    manager = JWTManager()
    manager.initialize()
    monkeypatch.setattr(routes, "jwt_manager", manager)
    monkeypatch.setattr(dependencies, "jwt_manager", manager)
    monkeypatch.setattr(routes, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(blogs, "check_rate_limit", lambda *args: None)
    factory = RepositoryFactory()
    await factory.initialize()
    app = FastAPI()
    app.state.factory = factory
    app.include_router(routes.router, prefix="/api/v1")
    app.include_router(blogs.router, prefix="/api/v1")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            payload = {"name": "Test User", "email": "local-test@example.com", "password": "Strong-local-pass9!"}
            signup = await client.post("/api/v1/auth/signup", json=payload)
            assert signup.status_code == 201, signup.text
            session = signup.json()
            uid = session["user"]["id"]
            headers = {"Authorization": "Bearer " + session["access_token"]}
            assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
            assert (await client.post("/api/v1/blogs", headers=headers, json={"title": "Title", "category": "News", "excerpt": "Body"})).status_code == 403
            await factory.user_repo.update_user(uid, {"role": "admin"})
            assert (await client.get("/api/v1/auth/me", headers=headers)).json()["role"] == "admin"
            await factory.user_repo.update_user(uid, {"role": "viewer"})
            assert (await client.post("/api/v1/blogs", headers=headers, json={"title": "Title", "category": "News", "excerpt": "Body"})).status_code == 403
            await factory.user_repo.set_user_active(uid, False)
            assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
            assert (await client.post("/api/v1/auth/login", json={"email": payload["email"], "password": payload["password"]})).status_code == 401
            assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]})).status_code == 401
    finally:
        await factory.db_manager.close()
