"""§28.7 — bounded, anonymous public link-preview metadata.

One responsibility: given a PUBLIC HTTP(S) URL, fetch a bounded amount
of metadata anonymously through the same outbound policy as every other
server-side fetch (SSRF guards, redirect revalidation, byte/time caps)
and return a sanitized display card. A domain-only card is a SUCCESSFUL
fallback — missing metadata is `limited`, never an error shown to the
user.

Hard guarantees the chat depends on:
- Hovering a link creates NO managed browser session, NO model usage,
  NO build and NO tool execution — this service performs exactly one
  bounded anonymous GET and nothing else.
- Private/internal destinations are refused at admission; the card for a
  private artifact URL is rendered from a per-request AUTHORIZED
  descriptor supplied by the caller (never from a shared cache).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 3
MAX_TOTAL_BYTES = 512 * 1024          # metadata extraction needs very little
FETCH_TIMEOUT_S = 6.0
CACHE_TTL_S = 300
SUPPORTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

PrivateDestination = ValueError

_TITLE_LIMIT = 300
_DESCRIPTION_LIMIT = 300


class LinkPreviewError(ValueError):
    """Raised for refused destinations; surfaced as an unavailable card."""


def _is_public_https(url: str) -> Tuple[bool, str]:
    """The same admission contract the SSRF guards enforce elsewhere:
    credential-free HTTPS, a hostname, no localhost/metadata/private
    target."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False, "unparsable URL"
    if parts.scheme != "https":
        return False, "only HTTPS destinations are previewed"
    if parts.username is not None or parts.password is not None:
        return False, "URL credentials are not permitted"
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False, "no destination host"
    if (host in ("localhost", "metadata.google.internal")
            or host.endswith((".local", ".internal"))
            or host == "localhost"):
        return False, "internal hosts are not previewed publicly"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        return False, "private network addresses are not previewed"
    return True, host


def _dns_resolves_public(host: str) -> bool:
    """Resolve through the OS resolver and refuse any private answer —
    DNS rebinding to a private address is a refusal, not a preview."""
    import socket
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            parsed = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not parsed.is_global:
            return False
    return True


class _MetadataParser(HTMLParser):
    """Collect only the metadata the card shows: title, description,
    og:image, favicon and the first heading as a last-resort title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.in_title = False
        self.in_heading = False
        self.heading_parts: List[str] = []
        self.description: Optional[str] = None
        self.og_image: Optional[str] = None
        self.icon: Optional[str] = None
        self._title_done = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        a = dict(attrs)
        if tag == "title" and not self._title_done:
            self.in_title = True
        elif tag in ("h1", "h2") and not self.heading_parts:
            self.in_heading = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content") or ""
            if name in ("description", "og:description") and content and self.description is None:
                self.description = content[:_DESCRIPTION_LIMIT]
            if name == "og:image" and content:
                self.og_image = content[:2000]
        elif tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href") or ""
            if "icon" in rel.split() and href and not self.icon:
                self.icon = href[:2000]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self.in_title:
            self.in_title = False
            self._title_done = True
        elif tag in ("h1", "h2"):
            self.in_heading = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        elif self.in_heading:
            self.heading_parts.append(data)


def _absolute(base: str, ref: str) -> Optional[str]:
    from urllib.parse import urljoin
    if not ref:
        return None
    if re.match(r"^https?://", ref, re.I):
        return ref
    if ref.startswith("//"):
        return "https:" + ref
    try:
        return urljoin(base, ref)
    except ValueError:
        return None


def _sanitize_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    if not collapsed:
        return None
    return collapsed[:_TITLE_LIMIT]


@dataclass
class LinkPreview:
    """The card payload. `state` is loading | ready | limited | unavailable."""
    requested_url: str
    display_url: str
    final_host: str
    state: str = "limited"
    title: Optional[str] = None
    description: Optional[str] = None
    favicon_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    content_type: Optional[str] = None
    fetched_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + CACHE_TTL_S)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "display_url": self.display_url,
            "final_host": self.final_host,
            "state": self.state,
            "title": self.title,
            "description": self.description,
            "favicon_url": self.favicon_url,
            "thumbnail_url": self.thumbnail_url,
            "content_type": self.content_type,
            "expires_at": self.expires_at,
        }


class LinkPreviewService:
    """Bounded anonymous metadata fetch. Intentionally TINY: no browser,
    no model, no build — one GET per preview."""

    def __init__(self, *, timeout_s: float = FETCH_TIMEOUT_S,
                 max_bytes: int = MAX_TOTAL_BYTES,
                 max_redirects: int = MAX_REDIRECTS) -> None:
        self.timeout_s = timeout_s
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        # 30.38: cache entries are PARTITIONED by principal (and, carried
        # in the key by callers, any authorization epoch/revision scoping)
        # so one account can never be served another account's cached
        # preview. Private descriptors are never cached at all.
        self._cache: Dict[str, Tuple[float, LinkPreview]] = {}

    @staticmethod
    def _cache_key(url: str, principal: str) -> str:
        return f"{principal}\x1f{url}"

    async def preview(self, url: str, *, refresh: bool = False,
                      principal: str = "public") -> LinkPreview:
        ok, host_or_reason = _is_public_https(url)
        if not ok:
            raise LinkPreviewError(f"destination not previewable: {host_or_reason}")
        key = self._cache_key(url, principal)
        if not refresh:
            cached = self._cache.get(key)
            if cached and cached[0] > time.time():
                return cached[1]
        preview = await self._fetch(url, host_or_reason)
        if preview.state in ("ready", "limited"):
            self._cache[key] = (preview.expires_at, preview)
        return preview

    def invalidate(self, url: str, *, principal: str = "public") -> None:
        self._cache.pop(self._cache_key(url, principal), None)

    async def _fetch(self, url: str, host: str) -> LinkPreview:
        # DNS is re-checked inside the service: a rebinding to a private
        # address is a refusal, never a fetch.
        if not await asyncio.to_thread(_dns_resolves_public, host):
            return LinkPreview(url, url, host, state="unavailable")
        current = url
        hops = 0
        body = b""
        final_url = url
        content_type = None
        try:
            while True:
                fetched = await self._one_get(current)
                if fetched is None:
                    return LinkPreview(url, url, host, state="unavailable")
                status, headers, chunk, final = fetched
                final_url = final
                if status in (301, 302, 303, 307, 308):
                    hops += 1
                    if hops > self.max_redirects:
                        break
                    loc = headers.get("location", "")
                    nxt = _absolute(current, loc)
                    if not nxt:
                        break
                    ok, new_host = _is_public_https(nxt)
                    if not ok:
                        # Cross-origin redirect to a non-public host: stop
                        # with what we have (a limited card), never fetch.
                        break
                    if not await asyncio.to_thread(_dns_resolves_public, new_host):
                        break
                    current = nxt
                    continue
                content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
                body = chunk
                break
        except asyncio.TimeoutError:
            return LinkPreview(url, url, host, state="unavailable")
        except Exception as exc:  # noqa: BLE001 — a preview must never raise
            logger.info("link preview fetch failed: %s", type(exc).__name__)
            return LinkPreview(url, url, host, state="unavailable")

        parts = urlsplit(final_url)
        final_host = (parts.hostname or host).rstrip(".").lower()
        if not body:
            return LinkPreview(url, final_url, final_host, state="limited",
                               content_type=content_type)
        if content_type and content_type not in SUPPORTED_CONTENT_TYPES:
            # A real non-HTML resource (image/PDF): the card still names
            # the destination honestly with a domain-only fallback.
            return LinkPreview(url, final_url, final_host, state="limited",
                               content_type=content_type)
        title, description, og_image, icon = self._parse_metadata(body)
        preview = LinkPreview(
            requested_url=url, display_url=final_url, final_host=final_host,
            state="ready" if title else "limited",
            title=title, description=description,
            content_type=content_type)
        if og_image:
            resolved = _absolute(final_url, og_image)
            if resolved and _is_public_https(resolved)[0]:
                preview.thumbnail_url = resolved
        if icon:
            resolved = _absolute(final_url, icon) or _absolute(final_url, "/favicon.ico")
            if resolved and _is_public_https(resolved)[0]:
                preview.favicon_url = resolved
        if not preview.favicon_url:
            preview.favicon_url = _absolute(final_url, "/favicon.ico")
        return preview

    def _parse_metadata(self, body: bytes) -> Tuple[Optional[str], Optional[str],
                                                   Optional[str], Optional[str]]:
        parser = _MetadataParser()
        try:
            parser.feed(body.decode("utf-8", errors="replace")[: self.max_bytes])
        except Exception:  # noqa: BLE001 — malformed HTML still gets a card
            pass
        title = _sanitize_text("".join(parser.title_parts)) or \
            _sanitize_text(" ".join(parser.heading_parts))
        return title, _sanitize_text(parser.description), parser.og_image, parser.icon

    async def _one_get(self, url: str):
        """One bounded anonymous GET via httpx with NO cookies/auth and a
        hard byte cap. Redirects are NEVER followed automatically — the
        caller re-validates each hop so a redirect can never smuggle a
        private destination into the preview."""
        import httpx
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=self.timeout_s,
                headers={"User-Agent": "AgentOSLinkPreview/1.0 (+https://agentos.example)"},
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        headers = dict(response.headers)
                        await response.aclose()
                        return response.status_code, headers, b"", url
                    ctype = response.headers.get("content-type", "")
                    # A declared content-length beyond the cap is refused
                    # before a single body byte is read.
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        await response.aclose()
                        return response.status_code, dict(response.headers), b"", url
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(16384):
                        buffer.extend(chunk)
                        if len(buffer) > self.max_bytes:
                            await response.aclose()
                            break
                    headers = dict(response.headers)
                    await response.aclose()
                    final = str(response.url)
                    return response.status_code, headers, bytes(buffer[: self.max_bytes]), final
        except httpx.HTTPError:
            return None


_service: Optional[LinkPreviewService] = None


def get_link_preview_service() -> LinkPreviewService:
    global _service
    if _service is None:
        _service = LinkPreviewService()
    return _service


def reset_link_preview_service() -> None:
    global _service
    _service = None


def authorized_private_descriptor(url: str, *, grant_check) -> Dict[str, Any]:
    """Build a per-request card for a PRIVATE resource descriptor (e.g. a
    workspace artifact URL). `grant_check(url)` returns True only when
    THIS requester is authorized for THIS resource — ownership is checked
    per request against the resource's own contract. Private descriptors
    are never cached and never fetched by this service; the card renders
    from the descriptor alone."""
    ok, _ = _is_public_https(url)
    if ok:
        raise LinkPreviewError(
            "authorized_private_descriptor is only for private descriptors")
    if not grant_check(url):
        raise LinkPreviewError("requester is not authorized for this resource")
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    path = parts.path or ""
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    kind = "document"
    if path.endswith(".pdf"):
        kind = "pdf"
    elif path.endswith((".obj", ".glb")):
        kind = "model"
    return {
        "requested_url": url,
        "display_url": path or url,
        "final_host": host or "workspace",
        "state": "ready",
        "title": name or "Workspace resource",
        "description": None,
        "favicon_url": None,
        "thumbnail_url": None,
        "content_type": {"pdf": "application/pdf"}.get(kind),
        "private": True,
        "expires_at": time.time() + 60,  # short-lived, per request
    }
