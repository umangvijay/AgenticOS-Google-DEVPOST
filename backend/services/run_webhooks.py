"""
AgentOS — Outbound Run Webhooks

POSTs a signed payload to a user-configured HTTPS URL when one of their
workflow runs reaches a terminal state (COMPLETED / FAILED).

Configuration (user settings):
    webhook_url: str   — public HTTPS endpoint. Empty/absent disables delivery.

Delivery is best-effort: signed with HMAC-SHA256 (x-agentos-signature,
x-agentos-timestamp), 3 attempts with short backoff, never blocks or
fails the run itself.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 10.0
DELIVERY_TOLERANCE_SECONDS = 300


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over 'timestamp.body' — the exact convention
    backend/security/webhook_signature.verify_signature expects."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


async def deliver_run_webhook(
    settings_repo,
    secrets_repo,
    user_id: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Fire the user's outbound webhook for a finished run. Returns delivery info."""
    try:
        user_settings = await settings_repo.get_settings(user_id) if settings_repo else {}
    except Exception:
        logger.exception("Could not load settings for webhook delivery")
        return None
    url = str((user_settings or {}).get("webhook_url") or "").strip()
    if not url:
        return None

    from backend.mcp.builder.openapi_parser import OpenAPIParser, SSRFViolationError
    try:
        OpenAPIParser()._validate_ssrf(url)
    except SSRFViolationError:
        logger.warning("Outbound webhook URL failed SSRF validation; not delivering")
        return {"ok": False, "error": "webhook_url must be a public HTTPS URL"}

    secret = "agentos-webhook"
    if secrets_repo is not None:
        try:
            from backend.security.secrets_vault import secrets_vault
            encrypted = await secrets_repo.get_secret(user_id, "webhook_out")
            if encrypted:
                secret = secrets_vault.decrypt(encrypted)
        except Exception:
            logger.warning("Could not load outbound webhook secret; using default", exc_info=True)

    body = json.dumps(payload, default=str).encode("utf-8")
    last_error: Optional[str] = None
    for attempt in range(MAX_ATTEMPTS):
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "x-agentos-timestamp": timestamp,
            "x-agentos-signature": sign_payload(secret, timestamp, body),
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
                response = await client.post(url, content=body, headers=headers)
            if response.status_code < 300:
                return {"ok": True, "status": response.status_code, "attempts": attempt + 1}
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = type(exc).__name__
        if attempt < MAX_ATTEMPTS - 1:
            await asyncio.sleep(1.0 * (2 ** attempt))
    logger.warning("Outbound webhook delivery failed after %d attempts: %s", MAX_ATTEMPTS, last_error)
    return {"ok": False, "error": last_error}
