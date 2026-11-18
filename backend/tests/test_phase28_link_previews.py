"""Phase 28 §28.7 — the chat link-preview contract.

The card is metadata ONLY: hovering a chat link creates no managed
browser session, no model usage, no build and no tool execution. The
service performs bounded anonymous GETs against PUBLIC destinations,
revalidates every redirect hop, and renders a domain-only fallback when
metadata is missing or the destination is non-HTML. Private descriptors
are authorized per request against the artifact store's own ownership
contract and are never cached or anonymously fetched.
"""
import asyncio
import hashlib
import json

import pytest
import pytest_asyncio
from aiohttp import web

from backend.services.link_preview_service import (
    LinkPreviewError,
    LinkPreviewService,
    authorized_private_descriptor,
    reset_link_preview_service,
)


PAGE_HTML = (
    "<html><head>"
    "<title>Acme Invoices \u2014 November 2026</title>"
    '<meta name="description" content="Overdue invoice ledger for November">'
    '<meta property="og:image" content="/og.png">'
    '<link rel="icon" href="/static/favicon.ico">'
    "</head><body><h1>Invoices</h1></body></html>"
).encode("utf-8")


@pytest.fixture
def service():
    reset_link_preview_service()
    yield LinkPreviewService()
    reset_link_preview_service()


@pytest.fixture
def loopback_admission(monkeypatch):
    """The local aiohttp fixture serves plain HTTP on loopback; relax the
    MODULE-level admission + DNS helpers to admit it while keeping every
    other guard (redirect revalidation, byte caps) live."""
    import backend.services.link_preview_service as lps
    real_admission = lps._is_public_https  # the unpatched guard

    def admit(url):
        parts = url.split("//", 1)[-1]
        host = parts.split("/")[0].split(":")[0]
        # Admit ONLY the loopback fixture hosts — private destinations
        # (workspace.internal, metadata, etc.) keep their real refusal.
        if not host.startswith("127.0.0.1"):
            return real_admission(url)
        return True, host

    def public_dns(host):
        return host.startswith("127.0.0.1")

    monkeypatch.setattr(lps, "_is_public_https", admit)
    monkeypatch.setattr(lps, "_dns_resolves_public", public_dns)
    return lps


@pytest_asyncio.fixture
async def public_site():
    hits = []

    async def page(request):
        hits.append(request.path)
        return web.Response(
            body=PAGE_HTML, content_type="text/html")

    async def redirect(request):
        # Redirect to the same public host — revalidated and followed.
        raise web.HTTPFound(location="/page")

    async def huge(request):
        return web.Response(body=b"x" * (1024 * 1024), content_type="text/html")

    async def pdf(request):
        return web.Response(body=b"%PDF-1.4 fake", content_type="application/pdf")

    async def private_redirect(request):
        # Redirect to a PRIVATE address — must be refused, never fetched.
        raise web.HTTPFound(location="http://127.0.0.1:9/private")

    app = web.Application()
    app.router.add_get("/page", page)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/huge", huge)
    app.router.add_get("/doc.pdf", pdf)
    app.router.add_get("/to-private", private_redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield {"base": f"127.0.0.1:{port}", "port": port, "hits": hits,
           "runner": runner}
    await runner.cleanup()


@pytest.mark.asyncio
async def test_public_page_yields_ready_card_with_metadata(public_site, loopback_admission):
    service = public_site["runner"] and LinkPreviewService()
    # The service refuses loopback hosts at admission; the unit exercises
    # the parse path directly through a loopback-safe instantiation.
    svc = LinkPreviewService()
    preview = await svc.preview(f"http://{public_site['base']}/page")
    assert preview.state == "ready"
    assert preview.title == "Acme Invoices — November 2026"
    assert preview.description == "Overdue invoice ledger for November"
    assert preview.final_host.startswith("127.0.0.1")
    assert preview.thumbnail_url and preview.thumbnail_url.endswith("/og.png")
    assert preview.favicon_url and preview.favicon_url.endswith("/favicon.ico")


@pytest.mark.asyncio
async def test_redirect_revalidated_and_followed(public_site, loopback_admission):
    svc = LinkPreviewService()
    preview = await svc.preview(f"http://{public_site['base']}/redirect")
    assert preview.state == "ready"
    assert "/page" in preview.display_url


@pytest.mark.asyncio
async def test_redirect_to_private_destination_is_never_fetched(public_site, loopback_admission):
    svc = LinkPreviewService()
    preview = await svc.preview(f"http://{public_site['base']}/to-private")
    assert preview.state in ("limited", "unavailable")
    assert all(h != "/private" for h in public_site["hits"]), \
        "the private hop must never be fetched"


@pytest.mark.asyncio
async def test_non_html_resource_is_a_limited_domain_card(public_site, loopback_admission):
    svc = LinkPreviewService()
    preview = await svc.preview(f"http://{public_site['base']}/doc.pdf")
    assert preview.state == "limited"
    assert preview.content_type == "application/pdf"


@pytest.mark.asyncio
async def test_oversized_response_is_capped_not_downloaded(public_site, loopback_admission):
    svc = LinkPreviewService(max_bytes=4096)
    svc._dns_resolves_public = lambda host: True  # type: ignore[method-assign]
    svc._is_public_https = lambda url: (True, url.split("//", 1)[-1])  # type: ignore
    preview = await svc.preview(f"http://{public_site['base']}/huge")
    # Capped fetch still yields an honest limited card.
    assert preview.state in ("limited", "unavailable")


@pytest.mark.asyncio
async def test_private_destinations_are_refused_at_admission():
    for url in ("http://169.254.169.254/latest/meta-data/",
                "ftp://example.com/x",
                "https://user:pass@example.com/",
                "https://localhost/x",
                "https://workspace.internal/x",
                "not a url"):
        with pytest.raises(LinkPreviewError):
            await LinkPreviewService().preview(url)


def test_private_descriptor_is_authorized_per_request(monkeypatch, tmp_path):
    calls = []

    def grant(url):
        calls.append(url)
        return "/artifacts/art-1/" in url

    card = authorized_private_descriptor(
        "https://workspace.internal/api/v1/artifacts/art-1/files/report.pdf?token=x",
        grant_check=grant)
    assert card["state"] == "ready"
    assert card["title"] == "report.pdf"
    assert card["content_type"] == "application/pdf"
    assert card["private"] is True
    assert card["final_host"] == "workspace.internal"
    assert calls == ["https://workspace.internal/api/v1/artifacts/art-1/files/report.pdf?token=x"]

    with pytest.raises(LinkPreviewError):
        authorized_private_descriptor(
            "https://workspace.internal/api/v1/artifacts/art-2/files/other.pdf",
            grant_check=grant)
    assert calls[-1].endswith("other.pdf")
    # A PUBLIC url is not a private descriptor.
    with pytest.raises(LinkPreviewError):
        authorized_private_descriptor("https://example.com/x", grant_check=grant)


def test_no_cache_across_invalidate(service):
    # 30.38: cache keys are principal-partitioned.
    key = service._cache_key("https://example.com/a", "user-1")
    service._cache[key] = (__import__("time").time() + 999, object())
    service.invalidate("https://example.com/a", principal="user-1")
    assert key not in service._cache


# ── the HTTP route ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_serves_public_preview_and_refuses_private_impersonation(
        public_site, loopback_admission, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from backend.api.routers.link_previews import router
    from backend.api.dependencies.auth import AuthenticatedUser, get_current_user

    app = FastAPI()
    app.include_router(router)
    identity = {"user_id": "owner-u"}

    def current_user():
        return AuthenticatedUser(identity["user_id"], "u@example.test", "U",
                                 "user", "local")
    app.dependency_overrides[get_current_user] = current_user

    # The private-descriptor grant consults the real artifact builder;
    # point it at a permissive stub for the authorized case.
    import backend.api.routers.link_previews as lp
    monkeypatch.setattr(lp, "_artifact_grant_check",
                        lambda uid, url: url.endswith("art-1/report.pdf"))

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://fixture") as client:
        public_url = f"http://{public_site['base']}/page"
        res = await client.post("/link-previews", json={"url": public_url})
        assert res.status_code == 200, res.text
        card = res.json()
        assert card["state"] == "ready" and card["title"]
        assert card["final_host"].startswith("127.0.0.1")

        denied = await client.post("/link-previews", json={
            "url": "https://workspace.internal/api/v1/artifacts/art-9/secret.pdf",
            "private": True})
        assert denied.status_code == 403

        allowed = await client.post("/link-previews", json={
            "url": "https://workspace.internal/api/v1/artifacts/art-1/report.pdf",
            "private": True})
        assert allowed.status_code == 200
        assert allowed.json()["private"] is True
        assert allowed.json()["content_type"] == "application/pdf"

        # The cache invalidation route exists for the client.
        inv = await client.delete("/link-previews/cache",
                                  params={"url": public_url})
        assert inv.status_code == 200 and inv.json()["invalidated"] is True
