"""Per-model-invocation metering (Phase 18 §18.7, finding C13).

The engine's outer runner reservation bounds the WHOLE execution; a
runner can make MULTIPLE model calls (tool-driven follow-ups). This
wrapper sits at the actual paid boundary: every `generate_content`
invocation opens its OWN metered_call with its own call identity
(run+task+model+invocation), captures that invocation's usage, and
settles on completion — retries/fallbacks INSIDE the client are part of
that invocation's attempt identity.

Distinctions tracked:
- logical task: run_id + task_id (the outer reservation)
- logical model call: one generate_content invocation (this wrapper)
- provider attempt/retry: inside the client wrapper (attempt_id)
- usage event: what the provider reports for this invocation
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("metered_model")

_METERED_ATTR = "_agentos_metered"


def wrap_model_with_metering(model: Any, *, user_id: str, run_id: str,
                             task_id: str, provider: str | None = None) -> Any:
    """Return a transparent wrapper whose generate_content calls each
    hold their own metered_call. Non-metered attributes delegate.
    §28.7: the provider is the RESOLVED one at dispatch — explicit
    argument, the wrapped model's own provider attribute, the process's
    resolved LLM provider, then the gemini-ADK default. A receipt that
    names the wrong provider is not usable for reconciliation."""
    if model is None or getattr(model, _METERED_ATTR, False):
        return model
    return _MeteredModel(model, user_id=user_id, run_id=run_id,
                         task_id=task_id, provider=provider)


def _resolved_provider() -> str | None:
    """The process-level resolved provider at wrap time (§28.7). Never
    raises: an unresolvable provider falls back to the caller's default."""
    try:
        from backend.services.llm_context import active_llm_provider
        name = active_llm_provider()
        return str(name) if name else None
    except Exception:
        return None


class _MeteredModel:
    _agentos_metered = True

    def __init__(self, inner: Any, *, user_id: str, run_id: str,
                 task_id: str, provider: str | None = None):
        self._inner = inner
        self._user_id = user_id
        self._run_id = run_id
        self._task_id = task_id
        self.invocations = 0
        self._provider = (provider
                          or getattr(inner, "_provider", None)
                          or getattr(inner, "provider", None)
                          or _resolved_provider()
                          or "gemini")
        # 30.32: the receipt names the model the provider actually saw.
        self._model_name = (getattr(inner, "model", None)
                            or getattr(inner, "_model_name", None)
                            or (inner if isinstance(inner, str) else None))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def __class__(self):
        """Report the INNER model's class to isinstance checks. The ADK's
        LlmAgent.canonical_model does `isinstance(self.model, BaseLlm)`
        and otherwise hands the object to LLMRegistry.new_llm, which does
        `':' in model` — a TypeError on a non-str wrapper (Phase 22: live
        worker crash found after a rebuild). With the delegation the ADK
        treats the wrapper AS the model and metering still owns every
        generate_content* call."""
        return type(self._inner)

    def _extract_text(self, response: Any) -> str:
        parts = []
        for cand in (getattr(response, "candidates", None) or []):
            for part in (getattr(getattr(cand, "content", None),
                                 "parts", None) or []):
                t = getattr(part, "text", None)
                if t:
                    parts.append(t)
        return "".join(parts)

    def _extract_usage(self, response: Any) -> Optional[Any]:
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            from backend.services.usage_normalizer import (
                normalize_gemini_usage,
            )
            try:
                normalized = normalize_gemini_usage(
                    usage, requested_model=self._model_name or "",
                    resolved_model=self._model_name,
                )
                request_id = (getattr(response, "response_id", None)
                              or getattr(response, "_response_id", None))
                if request_id and not normalized.provider_request_id:
                    normalized.provider_request_id = str(request_id)
                return normalized
            except Exception:
                return usage
        # OpenAI-compatible responses carry usage + an id on the payload.
        openai_usage = getattr(response, "usage", None)
        if openai_usage is not None or getattr(response, "id", None):
            from backend.services.usage_normalizer import (
                normalize_openai_usage,
            )
            try:
                normalized = normalize_openai_usage(
                    openai_usage, provider=self._provider,
                    requested_model=self._model_name,
                    resolved_model=getattr(response, "model", None)
                    or self._model_name,
                )
                request_id = getattr(response, "id", None)
                if request_id and not normalized.provider_request_id:
                    normalized.provider_request_id = str(request_id)
                return normalized
            except Exception:
                return openai_usage
        return None

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        """Sync invocation — the reservation is taken BEFORE dispatch
        (§19.5/C5): the sync path runs the SAME governed lifecycle as
        the async path on a dedicated loop — reserve with real bucket
        enforcement, persist the dispatch intent, dispatch, then settle
        once from the captured usage. A failed reservation means NO
        provider dispatch (§29.6). One dedicated connection serves the
        whole lifecycle and is closed before returning."""
        from backend.services.budget_service import BudgetService
        self.invocations += 1
        call_id = f"inv:{self._run_id}:{self._task_id}:{self.invocations}"
        prompt_text = ""
        for a in args:
            if isinstance(a, str):
                prompt_text += a
        manager = self._fresh_manager()
        try:
            repo = self._repo_for(manager)
            service = BudgetService(repo)
            # 1) RESERVE + persist dispatch intent — before dispatch.
            #    When accounting is unavailable the reservation fails
            #    closed: a FAILED reservation means NO provider dispatch.
            reservation = self._reserve_sync(
                service, call_id, prompt_text)
            if reservation is None:
                raise RuntimeError(
                    "budget reservation denied for this model invocation")
            # 2) DISPATCH.
            try:
                response = self._inner.generate_content(*args, **kwargs)
            except Exception:
                # Dispatch failed after reservation — uncertain (§2.6):
                # the provider may have consumed despite the lost reply.
                self._settle_sync(repo, reservation, None, uncertain=True)
                raise
            # 3) SETTLE once from the actual receipt.
            try:
                usage = self._extract_usage(response)
                self._settle_sync(repo, reservation, usage)
            except Exception:
                logger.warning(
                    "Per-invocation settlement failed", exc_info=True)
            return response
        finally:
            self._run_sync(manager.close())

    def _fresh_manager(self):
        """The sync path opens its OWN connection on the configured DB
        (aiosqlite binds connections to a loop — reusing the caller's
        repo across loops fails). The manager is initialized HERE on the
        caller's thread before any loop-specific use."""
        from backend.config.settings import settings
        from backend.repositories.sqlite.database import DatabaseManager
        manager = DatabaseManager(settings.SQLITE_DB_PATH)
        self._run_sync(manager.initialize())
        return manager

    def _repo_for(self, manager):
        from backend.repositories.usage_repository import (
            SQLiteUsageRepository)
        return SQLiteUsageRepository(manager)

    async def _reserve_and_mark_dispatching(self, service, call_id: str,
                                            prompt_text: str):
        """Reserve with REAL multi-bucket enforcement, then persist the
        dispatch intent — before the provider sees anything."""
        from backend.services.usage_normalizer import estimate_units_from_text
        reservation = await service.reserve(
            user_id=self._user_id,
            estimated_units=max(1, estimate_units_from_text(prompt_text)),
            provider=self._provider, purpose="model_invocation",
            run_id=self._run_id, task_id=self._task_id,
            call_id=call_id,
        )
        if reservation is None:
            return None
        await service.mark_dispatching(reservation.reservation_id)
        return reservation

    async def _settle_with_repo(self, repo, reservation, usage: Any,
                                uncertain: bool = False) -> None:
        from backend.models.usage import SettlementReceipt, MeasurementSource
        if uncertain:
            await repo.mark_uncertain(
                reservation.reservation_id, "provider response lost")
            return
        units = 0
        source = MeasurementSource.ESTIMATED
        if usage is not None and getattr(usage, "known", False):
            units = usage.policy_units() if hasattr(usage, "policy_units") \
                else 0
            source = MeasurementSource.PROVIDER_REPORTED
        await repo.settle(reservation.reservation_id, SettlementReceipt(
            reservation_id=reservation.reservation_id, units=units,
            measurement_source=source))

    def _run_sync(self, coro) -> Any:
        """Run one governed lifecycle coroutine on a DEDICATED loop.
        §28.7: when the caller is already inside a running loop (an async
        engine task), the dedicated loop runs on a helper thread instead
        of raising — the reservation/settlement lifecycle must work from
        both sync and async dispatch contexts."""
        import asyncio as _asyncio
        import threading as _threading
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        outcome: dict = {}

        def _worker():
            try:
                outcome["value"] = _asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001 — re-raised below
                outcome["error"] = exc

        thread = _threading.Thread(target=_worker, daemon=True,
                                   name="metered-lifecycle")
        thread.start()
        thread.join()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def _reserve_sync(self, service, call_id: str, prompt_text: str):
        """Governed synchronous reservation (§29.6): the same lifecycle
        coroutine the async path uses — multi-bucket enforcement inside
        the repo write lock, then the durable DISPATCHING transition —
        executed on a dedicated loop with a dedicated connection. A
        denied reservation returns None (no provider dispatch)."""
        try:
            return self._run_sync(self._reserve_and_mark_dispatching(
                service, call_id, prompt_text))
        except Exception as exc:
            logger.warning("Synchronous reservation failed: %s", exc)
            return None

    def _settle_sync(self, repo, reservation, usage: Any,
                     uncertain: bool = False) -> None:
        """Settle the sync reservation ONCE from the actual receipt (or
        mark uncertain when the response was lost after dispatch)."""
        self._run_sync(self._settle_with_repo(
            repo, reservation, usage, uncertain=uncertain))

    async def generate_content_async(self, *args: Any, **kwargs: Any):
        """Async invocation — an ASYNC GENERATOR of model responses.

        The ADK's LLM flow (and provider_readiness) consume the model
        with `async for response in model.generate_content_async(req)` —
        a plain coroutine here broke that protocol ('coroutine' object
        has no attribute 'aclose'; the agent then completed with an
        EMPTY reply). Each invocation holds its own reservation; the
        dispatch intent is persisted BEFORE the provider send, so a lost
        response settles as UNCERTAIN rather than released (§29.6).
        Non-generator inner models (a single awaited response) are
        adapted into a one-yield stream."""
        from backend.services.budget_service import get_budget_service
        from backend.services.usage_normalizer import (
            estimate_units_from_text,
        )
        prompt_text = ""
        for a in args:
            if isinstance(a, str):
                prompt_text += a
        service = get_budget_service()
        self.invocations += 1
        call_id = (f"inv:{self._run_id}:{self._task_id}:"
                   f"{self.invocations}")
        async with service.metered_call(
            user_id=self._user_id,
            estimated_input_units=estimate_units_from_text(prompt_text),
            provider=self._provider, purpose="model_invocation",
            run_id=self._run_id, task_id=self._task_id,
            call_id=call_id,
        ) as call:
            # §29.6: persist dispatch intent BEFORE the provider send.
            await call.mark_dispatched()
            produced = self._inner.generate_content_async(*args, **kwargs) \
                if hasattr(self._inner, "generate_content_async") \
                else self._inner.generate_content(*args, **kwargs)
            if hasattr(produced, "__aiter__"):
                async for response in produced:
                    usage = self._extract_usage(response)
                    if usage is not None:
                        call.capture_usage(usage)
                    text = self._extract_text(response)
                    if text:
                        call.capture_response_text(text)
                    yield response
            else:
                response = await produced
                usage = self._extract_usage(response)
                if usage is not None:
                    call.capture_usage(usage)
                text = self._extract_text(response)
                if text:
                    call.capture_response_text(text)
                yield response

