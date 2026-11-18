"""Shared outbound HTTP connection pool for workflow tasks.

core.http used to build a fresh httpx.AsyncClient per call — a new TCP +
TLS handshake for every task against the same API host. One pooled client
reuses keepalive connections (measurably faster MCP/API chains) while
per-call timeouts and the engine's redirect/SSRF rules stay unchanged.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# Each asyncio loop owns its pooled transports and shuts them down independently.
_POOL_ATTRIBUTE = "_agentos_outbound_http_client"

# Modest ceilings: workflow fan-out rarely exceeds a handful of hosts,
# and keepalive sockets are cheap to drop after 30s idle.
_limits = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)


async def get_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = getattr(loop, _POOL_ATTRIBUTE, None)
    if client is None or client.is_closed:
        # No await between lookup and publication on the owning loop.
        client = httpx.AsyncClient(limits=_limits, follow_redirects=False)
        setattr(loop, _POOL_ATTRIBUTE, client)
        logger.info("HTTP connection pool started")
    return client


async def aclose_client() -> None:
    loop = asyncio.get_running_loop()
    client = getattr(loop, _POOL_ATTRIBUTE, None)
    if client is not None:
        delattr(loop, _POOL_ATTRIBUTE)
        await client.aclose()
        logger.info("HTTP connection pool closed")
