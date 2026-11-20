"""
AgentOS — Website / endpoint health checker.

Live HTTPS probe of any public site: DNS, TLS, latency, status, redirects,
security headers, and HTML title. Used by the orchestrator and core.health.
Never targets private/loopback/metadata hosts.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SECURITY_HEADERS = (
    "content-security-policy",
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
)


class HealthCheckError(Exception):
    pass


def _is_private_host(host: str) -> bool:
    if not host:
        return True
    lowered = host.lower().rstrip(".")
    if lowered in ("localhost", "metadata.google.internal") or lowered.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        return False


def assert_public_https_url(url: str) -> str:
    from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
    try:
        OpenAPIParser()._validate_ssrf(url)
    except SSRFViolationError as exc:
        raise HealthCheckError(str(exc)) from exc
    return urlparse(url).hostname


def _tls_info(host: str, port: int = 443) -> Dict[str, Any]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                not_after = cert.get("notAfter")
                expires = None
                days_left = None
                if not_after:
                    expires_dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
                    expires = expires_dt.isoformat()
                    days_left = (expires_dt - datetime.now(timezone.utc)).days
                subject = dict(x[0] for x in cert.get("subject", ()))
                return {
                    "ok": True,
                    "tls_version": ssock.version(),
                    "subject_cn": subject.get("commonName"),
                    "expires_at": expires,
                    "days_until_expiry": days_left,
                }
    except Exception as e:
        return {"ok": False, "error": str(e).split("\n")[0][:200]}


def _grade(status_code: Optional[int], latency_ms: Optional[float], tls: Dict[str, Any], missing_headers: List[str]) -> str:
    if status_code is None:
        return "down"
    if status_code >= 500:
        return "unhealthy"
    if status_code >= 400:
        return "degraded"
    if latency_ms is not None and latency_ms > 3000:
        return "slow"
    if not tls.get("ok"):
        return "tls_issue"
    if missing_headers:
        return "ok_with_warnings"
    return "healthy"


async def check_website(url: str, timeout: float = 15.0, *, same_origin_only: bool = False) -> Dict[str, Any]:
    """Probe a public website and return a structured health report."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = await asyncio.to_thread(assert_public_https_url, url)

    started = time.perf_counter()
    status_code: Optional[int] = None
    final_url = url
    redirects: List[str] = []
    headers: Dict[str, str] = {}
    title = None
    body_bytes = 0
    error: Optional[str] = None

    try:
        from backend.config.settings import settings as app_settings
        probe_headers = {
            "User-Agent": getattr(app_settings, "HEALTH_USER_AGENT", None) or "SandtableHealth/2.0",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for hop in range(9):
                await asyncio.to_thread(assert_public_https_url, final_url)
                async with client.stream("GET", final_url, headers=probe_headers) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        if hop == 8:
                            raise HealthCheckError("Too many redirects")
                        destination = resp.url.join(resp.headers["location"])
                        if same_origin_only and (destination.scheme != "https" or destination.host != host
                                or (destination.port or 443) != (urlparse(url).port or 443)):
                            raise HealthCheckError("The page redirected outside the connector’s origin")
                        redirects.append(final_url)
                        final_url = str(destination)
                        continue
                    status_code = resp.status_code
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    chunks = []
                    async for chunk in resp.aiter_bytes():
                        body_bytes += len(chunk)
                        if body_bytes > 5 * 1024 * 1024:
                            raise HealthCheckError("Response exceeds the 5 MB probe limit")
                        chunks.append(chunk)
                    if "text/html" in (headers.get("content-type") or ""):
                        text = b"".join(chunks)[:80000].decode("utf-8", errors="replace")
                        match = TITLE_RE.search(text)
                        if match:
                            title = re.sub(r"\s+", " ", match.group(1)).strip()[:200]
                    break
    except Exception as e:
        error = str(e).split("\n")[0][:300]

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    tls = await asyncio.to_thread(_tls_info, host, urlparse(url).port or 443)
    present = [h for h in SECURITY_HEADERS if h in headers]
    missing = [h for h in SECURITY_HEADERS if h not in headers]
    grade = _grade(status_code, latency_ms, tls, missing)

    report = {
        "url": url,
        "data": None,
        "final_url": final_url,
        "host": host,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "grade": grade,
        "title": title,
        "bytes": body_bytes,
        "redirects": redirects,
        "tls": tls,
        "security_headers": {"present": present, "missing": missing},
        "error": error,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("Website health %s grade=%s status=%s latency=%sms", host, grade, status_code, latency_ms)
    return report
