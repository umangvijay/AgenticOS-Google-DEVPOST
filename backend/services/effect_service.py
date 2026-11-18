"""Effect service (Phase 5 §5.5/§5.6/§5.8) — the ONE execution boundary for
consequential external operations.

Sequence (§5.5): resolve authorization → prepare/retrieve the stable effect →
compare digest → verify approval → check cancellation → commit dispatch
intent → call the provider (NEVER inside a DB transaction) → persist the
receipt and outcome → return a STRUCTURED result (§5.6: a timeout is not a
failure; an SMTP partial acceptance is not a success).

Email is the first integrated path (§5.8): per-recipient acceptance vs
refusal is preserved, a stable Message-ID is generated before dispatch, and
SMTP timeout/disconnection preserves OUTCOME_UNKNOWN — never an automatic
resend (§5.16: unknown without a replay guarantee → no).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.models.admission import epoch_now

from backend.models.effect import (
    Effect,
    EffectDispatchResult,
    EffectOutcome,
    EffectReceipt,
    EffectState,
    EffectTransitionError,
    new_effect_attempt_id,
    new_effect_id,
)
from backend.repositories.effect_repository import (
    get_effect_repo,
    request_digest as compute_request_digest,
)

logger = logging.getLogger(__name__)

METRICS: Dict[str, int] = {}


def _incr(name: str, labels: Optional[Dict[str, str]] = None) -> None:
    key = name
    if labels:
        flat = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
        key = f"{name}{{{flat}}}"
    METRICS[key] = METRICS.get(key, 0) + 1


def effect_metrics() -> Dict[str, int]:
    return dict(METRICS)


# ── Email through the effect boundary (§5.8) ──────────────────────────

async def send_email_effect(
    *,
    secrets_repo,
    effect_repo,
    user_id: str,
    to: List[str] | str,
    subject: str,
    body: str,
    html: bool = False,
    from_name: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    command_id: Optional[str] = None,
    operation_instance_id: Optional[str] = None,
) -> EffectDispatchResult:
    """§5.8: send email THROUGH the effect ledger. Returns per-recipient
    truth: server acceptance ≠ final delivery; refused recipients are named;
    a lost connection preserves OUTCOME_UNKNOWN with
    retry_disposition='none' (plain SMTP has no replay guarantee — §5.16).
    """
    from backend.services.email_service import EmailError, EMAIL_PATTERN, SMTP_CREDENTIAL_NAME
    from backend.api.routers.credentials import load_credential

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise EmailError("No recipients provided")
    for addr in recipients:
        if not EMAIL_PATTERN.match(addr):
            raise EmailError(f"Invalid recipient address: {addr}")
    if not subject.strip():
        raise EmailError("Subject cannot be empty")

    try:
        smtp_cfg = await load_credential(secrets_repo, user_id, SMTP_CREDENTIAL_NAME)
    except ValueError:
        raise EmailError(
            "No SMTP credential configured. Store one via POST /api/v1/credentials "
            'with name "smtp" and fields: host, port, username, password.')
    missing = [f for f in ("host", "username", "password") if not smtp_cfg.get(f)]
    if missing:
        raise EmailError(f"SMTP credential is missing fields: {', '.join(missing)}")
    from_email = smtp_cfg.get("from_email") or smtp_cfg["username"]

    # §5.3: the canonical request binds recipients, subject, body digest —
    # changing ANY of them is a detectable action change.
    semantic_args = {
        "recipients": sorted(recipients),
        "subject": subject,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "html": bool(html),
        "from": from_email,
    }
    digest = compute_request_digest(user_id, "email.send", semantic_args)
    effect_id = new_effect_id()

    async def _smtp_send(message: EmailMessage) -> Dict[str, List[str]]:
        """The transport: returns per-recipient acceptance (smtplib's
        send_message return value — refused recipients are NORMAL returns,
        §5.8). Raises on connection-level failure (outcome unknown)."""
        import smtplib
        import ssl
        host = smtp_cfg["host"]
        port = int(smtp_cfg.get("port", 587))
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                server.login(smtp_cfg["username"], smtp_cfg["password"])
                return server.send_message(message)
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            server.login(smtp_cfg["username"], smtp_cfg["password"])
            return server.send_message(message)

    return await dispatch_effect(
        effect_repo=effect_repo,
        operation_name="email.send",
        owner_user_id=user_id,
        run_id=run_id, task_id=task_id, command_id=command_id,
        operation_instance_id=operation_instance_id,
        external_account_id=from_email,
        target=", ".join(sorted(recipients)),
        semantic_args=semantic_args,
        request_digest=digest,
        provider="smtp",
        effect_id=effect_id,
        dispatch=partial_send_email_dispatch(
            smtp_cfg, recipients, subject, body, html, from_email, from_name,
            _smtp_send),
        classify=classify_smtp_outcome(recipients),
    )


def partial_send_email_dispatch(smtp_cfg, recipients, subject, body, html,
                                from_email, from_name, smtp_send):
    """§5.5 step 9-11: the network call happens OUTSIDE any transaction and
    reports per-recipient acceptance through the receipt."""

    async def dispatch(effect: Effect, attempt_id: str) -> Dict[str, List[str]]:
        """§5.5 steps 9-11: the network call happens OUTSIDE any DB
        transaction. smtplib's send_message returns refused recipients as a
        NORMAL return (§5.8 partial truth); connection failures raise and the
        boundary preserves OUTCOME_UNKNOWN (§5.6)."""
        msg = EmailMessage()
        msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        # §5.8: a stable Message-ID generated BEFORE dispatch for correlation
        # (correlation, not a dedup guarantee).
        msg_id = make_msgid(domain=from_email.split("@")[-1])
        msg["Message-ID"] = msg_id
        if html:
            msg.set_content("This email requires an HTML-capable client.")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)
        import asyncio
        refused = await asyncio.to_thread(smtp_send, msg)
        return refused, msg_id

    return dispatch


def classify_smtp_outcome(recipients: List[str],
                          from_email: str = "") -> Callable[
        [tuple, str], EffectDispatchResult]:
    """§5.8: SMTP accepted-for-some is a PARTIAL outcome; refused
    recipients are named; server acceptance is NOT delivery confirmation."""

    def classify(result: tuple, effect_id: str) -> EffectDispatchResult:
        refused, message_id = result
        accepted = [r for r in recipients if r not in (refused or {})]
        refused_list = sorted((refused or {}).keys())
        if refused and accepted:
            outcome, state = EffectOutcome.PARTIALLY_APPLIED, EffectState.PARTIALLY_APPLIED
            message = (f"Accepted for {len(accepted)} recipient(s); "
                       f"{len(refused_list)} refused: {', '.join(refused_list)}.")
        elif refused:
            outcome, state = (EffectOutcome.CONFIRMED_NOT_APPLIED,
                              EffectState.CONFIRMED_NOT_APPLIED)
            message = f"All recipients refused by the mail server: {', '.join(refused_list)}."
        else:
            outcome, state = EffectOutcome.CONFIRMED_APPLIED, EffectState.CONFIRMED_APPLIED
            message = "Accepted by your mail server (delivery not yet confirmed)."

        return EffectDispatchResult(
            outcome=outcome,
            effect_id="",
            receipt=EffectReceipt(
                effect_id="", effect_attempt_id="", provider="smtp",
                external_account_scope=from_email,
                provider_operation_id=message_id,
                provider_status="smtp_accepted",
                detail={"accepted": accepted, "refused": refused_list,
                        "note": "Server acceptance; final delivery not confirmed"},
            ),
            # §5.16: refused recipients may be retried EXPLICITLY; unknown is
            # never auto-retried (plain SMTP has no replay guarantee).
            retry_disposition="retry_items" if refused and accepted else "none",
            safe_user_message=message,
        )

    return classify


# ── The general boundary (§5.5) ───────────────────────────────────────

async def dispatch_effect(
    *,
    effect_repo,
    operation_name: str,
    owner_user_id: str,
    semantic_args: Dict[str, Any],
    request_digest: str,
    provider: str,
    dispatch: Callable[[Effect, str], Awaitable[Any]],
    classify: Callable[[Any, str], EffectDispatchResult],
    target: str = "",
    external_account_id: Optional[str] = None,
    credential_reference: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    command_id: Optional[str] = None,
    operation_instance_id: Optional[str] = None,
    effect_id: Optional[str] = None,
    connector_id: Optional[str] = None,
    connector_revision: Optional[str] = None,
    approval_reference: Optional[str] = None,
) -> EffectDispatchResult:
    """§5.5 — the ONE effect-execution boundary:
    prepare-or-retrieve → digest compare → dispatch intent → provider call
    (outside any DB transaction) → receipt + outcome → structured result.

    Crash boundaries (§5.6): DISPATCHING committed then a crash = the remote
    MAY have happened — recovery treats it as potentially dispatched;
    response-received-but-receipt-write-fails preserves uncertainty."""
    if effect_repo is None:
        raise RuntimeError("Effect repository not configured")

    effect = Effect(
        effect_id=effect_id or new_effect_id(),
        operation_name=operation_name, owner_user_id=owner_user_id,
        run_id=run_id, task_id=task_id, command_id=command_id,
        operation_instance_id=operation_instance_id,
        connector_id=connector_id, connector_revision=connector_revision,
        external_account_id=external_account_id,
        credential_reference=credential_reference,
        target=target, canonical_arguments=semantic_args,
        request_digest=request_digest,
        approval_reference=approval_reference,
    )
    effect, created = await effect_repo.prepare_effect(effect)

    if not created:
        # §5.2 rule 1: a transport retry preserves the effect. An UNSETTLED
        # duplicate submission returns the existing effect honestly.
        _incr("effects_deduplicated", {"operation": operation_name})
        return EffectDispatchResult(
            outcome=EffectOutcome.OUTCOME_UNKNOWN if effect.state in (
                EffectState.DISPATCHING, EffectState.OUTCOME_UNKNOWN,
                EffectState.PROVIDER_PENDING) else _state_to_outcome(effect.state),
            effect_id=effect.effect_id,
            retry_disposition="none",
            reconciliation_required=effect.state in (
                EffectState.DISPATCHING, EffectState.OUTCOME_UNKNOWN),
            safe_user_message=(
                "This exact action was already recorded "
                f"({effect.state.value}); not submitting again. Inspect its "
                "outcome via the effect history."))

    attempt_id = new_effect_attempt_id()
    try:
        await effect_repo.transition(effect.effect_id, EffectState.DISPATCHING,
                                     actor="effect_service",
                                     reason="dispatch intent committed (§5.5 step 9)")
    except EffectTransitionError:
        pass   # raced to a terminal state (cancellation): do not dispatch

    try:
        result = await dispatch(effect, attempt_id)
    except Exception as exc:
        # §5.6: during transmission / provider-committed-response-lost →
        # OUTCOME_UNKNOWN; never assume failure, never blind-retry.
        _incr("effects_unknown", {"operation": operation_name})
        try:
            await effect_repo.transition(
                effect.effect_id, EffectState.OUTCOME_UNKNOWN,
                actor="effect_service",
                reason=f"transport outcome unknown: {type(exc).__name__}")
        except EffectTransitionError:
            pass
        return EffectDispatchResult(
            outcome=EffectOutcome.OUTCOME_UNKNOWN,
            effect_id=effect.effect_id, effect_attempt_id=attempt_id,
            retry_disposition="none",      # §5.16: no replay guarantee → no
            reconciliation_required=True,
            safe_user_message=(
                "The connection ended before the result could be confirmed. "
                "The action may have been submitted. Automatic resend is "
                "paused while the outcome is checked."))

    classified = classify(result, effect.effect_id)
    classified.effect_id = effect.effect_id
    classified.effect_attempt_id = attempt_id
    if classified.receipt is not None:
        receipt = classified.receipt
        receipt.effect_id = effect.effect_id
        receipt.effect_attempt_id = attempt_id
        await effect_repo.record_receipt(receipt)
    target_state = {
        EffectOutcome.CONFIRMED_APPLIED: EffectState.CONFIRMED_APPLIED,
        EffectOutcome.CONFIRMED_NOT_APPLIED: EffectState.CONFIRMED_NOT_APPLIED,
        EffectOutcome.PARTIALLY_APPLIED: EffectState.PARTIALLY_APPLIED,
        EffectOutcome.PROVIDER_PENDING: EffectState.PROVIDER_PENDING,
        EffectOutcome.OUTCOME_UNKNOWN: EffectState.OUTCOME_UNKNOWN,
    }.get(classified.outcome)
    if target_state is not None:
        try:
            await effect_repo.transition(effect.effect_id, target_state,
                                         actor="effect_service",
                                         reason=f"provider outcome: {classified.outcome.value}")
        except EffectTransitionError:
            pass
    _incr("effects_dispatched", {"operation": operation_name,
                                 "outcome": classified.outcome.value})
    return classified


def _state_to_outcome(state: EffectState) -> EffectOutcome:
    return {
        EffectState.CONFIRMED_APPLIED: EffectOutcome.CONFIRMED_APPLIED,
        EffectState.CONFIRMED_NOT_APPLIED: EffectOutcome.CONFIRMED_NOT_APPLIED,
        EffectState.PARTIALLY_APPLIED: EffectOutcome.PARTIALLY_APPLIED,
        EffectState.PROVIDER_PENDING: EffectOutcome.PROVIDER_PENDING,
    }.get(state, EffectOutcome.OUTCOME_UNKNOWN)


async def dispatch_connector_write_effect(
    *,
    effect_repo,
    user_id: str,
    connector_id: str,
    connector_name: str,
    tool_name: str,
    method: str,
    url: str,
    body,
    revision: Optional[str] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    command_id: Optional[str] = None,
    do_request,
) -> EffectDispatchResult:
    """§29.7: a CONSEQUENTIAL connector operation (POST/PUT/PATCH/DELETE)
    goes through the effect ledger — one logical write per (operation,
    canonical arguments), the dispatch intent committed BEFORE the HTTP
    call, a lost reply preserved as OUTCOME_UNKNOWN with automatic
    redispatch suppressed. `do_request` performs the actual HTTP request
    and returns a dict {status, body} or raises on transport failure.

    Classification (evidence-based, never optimistic):
    - 2xx            → CONFIRMED_APPLIED (provider accepted the write);
    - 4xx (non-408/429) → CONFIRMED_NOT_APPLIED (rejected before applying);
    - 408/429/5xx    → OUTCOME_UNKNOWN (the provider may have applied it);
    - transport error → OUTCOME_UNKNOWN (handled by dispatch_effect)."""
    semantic_args = {
        "connector": connector_name,
        "tool": tool_name,
        "method": str(method).upper(),
        "url": str(url),
        # The body digest dedups without retaining large payloads in the
        # canonical record; the raw body stays inside the receipt only.
        "body_sha256": hashlib.sha256(
            json.dumps(body, sort_keys=True, default=str).encode()
        ).hexdigest() if body is not None else None,
    }
    operation_name = f"connector:{connector_id}:{tool_name}"
    digest = compute_request_digest(user_id, operation_name, semantic_args)

    async def _dispatch(effect: Effect, attempt_id: str):
        result = await do_request()
        receipt = EffectReceipt(
            effect_id=effect.effect_id,
            effect_attempt_id=attempt_id,
            provider="http_connector",
            provider_status=f"http_{result.get('status')}",
            observed_at=epoch_now(),
        )
        return result, receipt

    def _classify(result_with_receipt, effect_id: str) -> EffectDispatchResult:
        result, receipt = result_with_receipt
        status_code = int(result.get("status") or 0)
        if 200 <= status_code < 300:
            outcome = EffectOutcome.CONFIRMED_APPLIED
        elif 400 <= status_code < 500 and status_code not in (408, 429):
            outcome = EffectOutcome.CONFIRMED_NOT_APPLIED
        else:
            outcome = EffectOutcome.OUTCOME_UNKNOWN
        return EffectDispatchResult(
            outcome=outcome,
            effect_id=effect_id,
            effect_attempt_id=receipt.effect_attempt_id,
            receipt=receipt,
            retry_disposition="none",
            reconciliation_required=outcome is EffectOutcome.OUTCOME_UNKNOWN,
            safe_user_message={
                EffectOutcome.CONFIRMED_APPLIED:
                    "The connector operation was applied.",
                EffectOutcome.CONFIRMED_NOT_APPLIED:
                    "The connector refused the operation before applying it.",
            }.get(outcome, "The connector outcome is UNKNOWN — the request "
                            "may have been applied; inspect the effect "
                            "history before retrying."),
        )

    return await dispatch_effect(
        effect_repo=effect_repo,
        operation_name=operation_name,
        owner_user_id=user_id,
        semantic_args=semantic_args,
        request_digest=digest,
        provider="http_connector",
        dispatch=_dispatch,
        classify=_classify,
        target=url,
        connector_id=connector_id,
        connector_revision=revision,
        run_id=run_id, task_id=task_id, command_id=command_id,
    )
