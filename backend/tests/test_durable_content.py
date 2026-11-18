from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import asyncio

import pytest
import pytest_asyncio
from pydantic import ValidationError

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.content_repository import SQLiteContentRepository
from backend.api.routers.contact import ContactRequest, submit_contact
from backend.api.routers.blogs import BlogPost
from backend.security.rate_limiter import SlidingWindowCounter, get_client_identifier


@pytest.mark.asyncio
async def test_posts_survive_restart_and_contacts_never_become_public(tmp_path):
    path = str(tmp_path / "content.db")
    db = DatabaseManager(path)
    await db.initialize()
    repo = SQLiteContentRepository(db)
    await asyncio.gather(*(repo.create("blog", {"title": str(i)}) for i in range(12)))
    await repo.create("contact", {"message": "private"})
    await db.close()
    reopened = DatabaseManager(path)
    await reopened.initialize()
    try:
        posts = await SQLiteContentRepository(reopened).list_public_posts()
        assert len(posts) == 12
        assert {row["title"] for row in posts} == {str(i) for i in range(12)}
        assert all("message" not in row for row in posts)
    finally:
        await reopened.close()


@pytest.mark.parametrize("payload", [{"email": "x@y.com", "message": " "}, {"email": "x@y.com", "message": "x" * 8001}])
def test_contact_rejects_empty_and_oversized_fields(payload):
    with pytest.raises(ValidationError): ContactRequest(**payload)


def test_blog_requires_bounded_strings():
    with pytest.raises(ValidationError): BlogPost(title={"bad": "object"}, category="News", excerpt="Text")
    with pytest.raises(ValidationError): BlogPost(title=" ", category="News", excerpt="Text")


def test_forwarded_header_cannot_choose_limiter_identity():
    request = SimpleNamespace(headers={"X-Forwarded-For": "fake-client"}, client=SimpleNamespace(host="192.0.2.1"))
    assert get_client_identifier(request) == "ip:192.0.2.1"


def test_limiter_cardinality_is_bounded():
    limiter = SlidingWindowCounter(max_keys=2)
    assert limiter.check_rate_limit("a", "auth", 3)[0]
    assert limiter.check_rate_limit("b", "auth", 3)[0]
    assert not limiter.check_rate_limit("c", "auth", 3)[0]
    assert len(limiter._windows) == 2


@pytest.mark.asyncio
async def test_contact_does_not_send_when_durable_save_fails(monkeypatch):
    from backend.api.routers import contact
    from fastapi import HTTPException
    monkeypatch.setattr(contact, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(contact, "get_client_identifier", lambda _: "test")
    monkeypatch.setattr(contact, "_contact_to", lambda: "team@example.com")
    send = Mock()
    monkeypatch.setattr(contact, "_send_smtp", send)
    factory = SimpleNamespace(content_repo=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("store down"))))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=factory)))
    with pytest.raises(HTTPException) as error:
        await submit_contact(ContactRequest(email="x@y.com", message="Hello"), request)
    assert error.value.status_code == 500
    send.assert_not_called()
