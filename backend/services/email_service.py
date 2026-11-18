"""
AgentOS — Email Service

Sends real email on the user's behalf via their own SMTP account
(e.g. Gmail with an app password). SMTP settings are stored as a
vault-encrypted credential named "smtp" with fields:

    host      — e.g. "smtp.gmail.com"
    port      — e.g. "587" (STARTTLS) or "465" (SSL)
    username  — SMTP login
    password  — SMTP password / app password
    from_email (optional) — defaults to username

Used by the orchestrator's send_email tool and the core.email DAG node.
"""

import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional, Any, Dict, List, Optional

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SMTP_CREDENTIAL_NAME = "smtp"


class EmailError(Exception):
    pass


def _send_sync(
    smtp_cfg: Dict[str, str], to: List[str], subject: str,
    body: str, html: bool, from_email: str, from_name: Optional[str],
) -> None:
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 587))
    username = smtp_cfg["username"]
    password = smtp_cfg["password"]

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    if html:
        msg.set_content("This email requires an HTML-capable client.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(username, password)
            server.send_message(msg)


async def send_email(
    secrets_repo,
    user_id: str,
    to: List[str] | str,
    subject: str,
    body: str,
    html: bool = False,
    from_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an email using the user's stored SMTP credential."""
    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailError("No recipients provided")
    for addr in recipients:
        if not EMAIL_PATTERN.match(addr):
            raise EmailError(f"Invalid recipient address: {addr}")
    if not subject.strip():
        raise EmailError("Subject cannot be empty")

    from backend.api.routers.credentials import load_credential
    try:
        smtp_cfg = await load_credential(secrets_repo, user_id, SMTP_CREDENTIAL_NAME)
    except ValueError:
        raise EmailError(
            "No SMTP credential configured. Store one via POST /api/v1/credentials "
            'with name "smtp" and fields: host, port, username, password.'
        )

    missing = [f for f in ("host", "username", "password") if not smtp_cfg.get(f)]
    if missing:
        raise EmailError(f"SMTP credential is missing fields: {', '.join(missing)}")

    from_email = smtp_cfg.get("from_email") or smtp_cfg["username"]

    # Phase 5 §5.8: sending goes through the EFFECT boundary — per-recipient
    # acceptance/refusal preserved, a stable Message-ID correlates the send,
    # and a lost connection yields OUTCOME_UNKNOWN (never an automatic
    # resend, and never a blanket "sent": true).
    from backend.repositories.effect_repository import get_effect_repo
    from backend.services.effect_service import send_email_effect
    effect_repo = get_effect_repo()
    if effect_repo is None:
        raise EmailError("Effect ledger unavailable; refusing to send without "
                         "a durable record of the intended external action")

    result = await send_email_effect(
        secrets_repo=secrets_repo, effect_repo=effect_repo, user_id=user_id,
        to=recipients, subject=subject, body=body, html=html,
        from_name=from_name, run_id=_current_run_id(), task_id=_current_task_id(),
        command_id=_current_command_id(),
    )

    if result.outcome.value == "outcome_unknown":
        logger.warning("Email outcome UNKNOWN for user %s (effect %s) — no "
                       "automatic resend (§5.16)", user_id, result.effect_id)
        return {
            "sent": False,
            "outcome": "unknown",
            "effect_id": result.effect_id,
            "to": recipients,
            "subject": subject,
            "message": result.safe_user_message,
        }
    if result.outcome.value == "partially_applied":
        receipt = result.receipt or {}
        detail = getattr(receipt, "detail", {}) or {}
        return {
            "sent": True,
            "outcome": "partial",
            "effect_id": result.effect_id,
            "to": detail.get("accepted", recipients),
            "refused": detail.get("refused", []),
            "subject": subject,
            "from": from_email,
            "message": result.safe_user_message,
        }
    if result.outcome.value == "confirmed_not_applied":
        detail = getattr(result.receipt, "detail", {}) or {}
        return {
            "sent": False,
            "outcome": "refused",
            "effect_id": result.effect_id,
            "refused": detail.get("refused", recipients),
            "subject": subject,
            "from": from_email,
            "message": result.safe_user_message,
        }
    logger.info("Email accepted for %d recipient(s) for user %s (effect %s)",
                len(recipients), user_id, result.effect_id)
    return {
        "sent": True,
        "outcome": "accepted",
        "effect_id": result.effect_id,
        "to": recipients,
        "subject": subject,
        "from": from_email,
        "message": result.safe_user_message,
    }


def _current_run_id() -> Optional[str]:
    try:
        from backend.services.llm_context import get_run_id
        return get_run_id()
    except Exception:
        return None


def _current_task_id() -> Optional[str]:
    try:
        from backend.services.llm_context import get_task_id
        return get_task_id()
    except Exception:
        return None


def _current_command_id() -> Optional[str]:
    """C4 bridge: the command linked to the current run (best-effort; the
    effect keeps run_id regardless, and the command is reachable by run)."""
    return None
