"""Google ADK bridge to Hugging Face's OpenAI-compatible inference router."""

import json
import logging
from typing import AsyncGenerator

import httpx
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from backend.config.settings import settings
from backend.services.groq_adk import GroqADK, _request_messages, _request_tools, _response_schema
from backend.services.llm_context import effective_hf_token

logger = logging.getLogger(__name__)


class HuggingFaceADK(GroqADK):
    """ADK model adapter supporting planner schemas and real function/tool calls."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        token = effective_hf_token()
        if not token:
            raise RuntimeError("Hugging Face provider selected but HF_TOKEN is not configured")

        config = llm_request.config
        model = self.model or settings.HF_MODEL
        payload: dict = {
            "model": model,
            "messages": _request_messages(llm_request),
        }
        temperature = getattr(config, "temperature", None) if config else None
        if temperature is not None:
            payload["temperature"] = temperature
        # Phase 4 §4.0C (R6): bound the provider's output to the RESERVED
        # output allowance — the hold covers what the provider may generate.
        payload["max_tokens"] = int(settings.MAX_OUTPUT_RESERVE_TOKENS)
        tools = _request_tools(llm_request)
        if tools:
            payload["tools"] = tools
        schema = _response_schema(config) if config else None
        if schema and not tools:
            payload["response_format"] = {"type": "json_schema", "json_schema": schema}
        elif config and getattr(config, "response_mime_type", None) == "application/json":
            payload["response_format"] = {"type": "json_object"}

        # Phase 2 (§2.11): same accounting contract as every other adapter —
        # one durable reservation per model invocation at the model boundary.
        from backend.services.budget_service import get_budget_service
        from backend.services.usage_normalizer import (
            estimate_units_from_text, normalize_openai_usage,
        )
        from backend.services.llm_context import get_run_id, get_user_id, get_org_id

        estimate = estimate_units_from_text(json.dumps(payload["messages"], default=str))
        estimate += estimate_units_from_text(json.dumps(tools, default=str)) if tools else 0
        service = get_budget_service()
        async with service.metered_call(
            user_id=get_user_id() or "platform-guest",
            estimated_input_units=estimate,
            provider="huggingface",
            model=model,
            purpose="adk_generation",
            run_id=get_run_id(),
            organization_id=get_org_id(),
        ) as call:
            await call.mark_dispatched()
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
                response = await client.post(
                    "https://router.huggingface.co/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload,
                )
            if response.status_code >= 400:
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after") if response.headers else None
                    suffix = f"; retry after {retry_after}s" if retry_after and retry_after.isdigit() else ""
                    raise RuntimeError(f"Hugging Face inference rate limit reached (HTTP 429{suffix})")
                if response.status_code == 402:
                    raise RuntimeError("Hugging Face inference credits or billing are unavailable (HTTP 402)")
                if response.status_code == 403:
                    raise RuntimeError("Hugging Face token lacks Inference Providers access (HTTP 403)")
                if response.status_code == 401:
                    raise RuntimeError("Hugging Face rejected the configured token (HTTP 401)")
                raise RuntimeError(f"Hugging Face inference failed (HTTP {response.status_code})")

            data = response.json()
            call.capture_usage(normalize_openai_usage(
                data.get("usage"), provider="huggingface", requested_model=model,
            ))
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            parts = []
            if message.get("content"):
                parts.append(types.Part(text=message["content"]))
                call.capture_response_text(message["content"])
            for tool_call in message.get("tool_calls") or []:
                fn = tool_call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError("Hugging Face returned invalid JSON for a tool call") from exc
                parts.append(types.Part(function_call=types.FunctionCall(
                    id=tool_call.get("id"), name=fn.get("name"), args=args,
                )))
            if not parts:
                raise ValueError("Hugging Face returned an empty model response")

            logger.info("ADK model call completed with Hugging Face model %s", model)
            yield LlmResponse(
                content=types.Content(role="model", parts=parts),
                model_version=model,
                partial=False,
            )
