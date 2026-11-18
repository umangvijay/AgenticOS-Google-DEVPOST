"""Current identity and fail-closed revocation regression tests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend.api.dependencies import auth
from backend.api.routers import auth as routes


def request(account):
    factory = SimpleNamespace(
        user_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=account),
                                  get_by_email=AsyncMock(return_value=account),
                                  get_by_google_id=AsyncMock(return_value=account)),
        token_denylist_repo=SimpleNamespace(is_revoked=AsyncMock(return_value=False)),
    )
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=factory)))


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [None, {"is_active": False}])
async def test_disabled_or_deleted_account_rejected_with_existing_token(monkeypatch, account):
    monkeypatch.setattr(auth.jwt_manager, "verify_access_token", lambda _: {"sub": "u", "jti": "j", "role": "admin"})
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(request(account), HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_demotion_takes_effect_on_next_request(monkeypatch):
    monkeypatch.setattr(auth.jwt_manager, "verify_access_token", lambda _: {"sub": "u", "jti": "j", "role": "admin"})
    user = await auth.get_current_user(request({"is_active": True, "role": "viewer"}),
                                      HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"))
    assert user.role == "viewer"
    assert not user.is_admin()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_revocation_store_failure_denies_request(monkeypatch, missing):
    monkeypatch.setattr(auth.jwt_manager, "verify_access_token", lambda _: {"sub": "u", "jti": "j"})
    req = request({"is_active": True})
    if missing:
        req.app.state.factory.token_denylist_repo = None
    else:
        req.app.state.factory.token_denylist_repo.is_revoked.side_effect = RuntimeError("unavailable")
    with pytest.raises(HTTPException) as error:
        await auth.get_current_user(req, HTTPAuthorizationCredentials(scheme="Bearer", credentials="token"))
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_disabled_password_login_never_issues_tokens(monkeypatch):
    monkeypatch.setattr(routes, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(routes, "get_client_identifier", lambda _: "test")
    issue = Mock()
    monkeypatch.setattr(routes.jwt_manager, "create_token_pair", issue)
    with pytest.raises(HTTPException) as error:
        await routes.login(SimpleNamespace(email="disabled@example.com", password="GoodPass1!"),
                           request({"id": "u", "is_active": False, "password_hash": "hash"}))
    assert error.value.status_code == 401
    issue.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_google_login_never_issues_tokens(monkeypatch):
    monkeypatch.setattr(routes, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(routes, "get_client_identifier", lambda _: "test")
    monkeypatch.setattr(routes, "verify_google_id_token", Mock(return_value={"google_id": "g", "email": "disabled@example.com"}))
    issue = Mock()
    monkeypatch.setattr(routes.jwt_manager, "create_token_pair", issue)
    with pytest.raises(HTTPException) as error:
        await routes.google_auth(SimpleNamespace(id_token="token", code=None),
                                 request({"id": "u", "is_active": False}))
    assert error.value.status_code == 401
    issue.assert_not_called()
