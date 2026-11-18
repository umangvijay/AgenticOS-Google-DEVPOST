"""Google ADK BaseLlm adapter for Groq's OpenAI-compatible chat API.

This keeps the workflow planner and tool-using orchestrator usable when the
deployment is configured with Groq instead of Gemini/Vertex. Credentials are
resolved per request from the context-local vault key or process settings and
are never included in diagnostics.
"""

import json
import logging
from typing import AsyncGenerator, ClassVar, Optional

import httpx
from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from backend.config.settings import settings
from backend.services.llm_context import effective_groq_key

logger = logging.getLogger(__name__)


def _instruction_text(value) -> str:
    if isinstance(value, str):
        return value
    parts = getattr(value, "parts", None) or []
    return "\n".join(p.text for p in parts if getattr(p, "text", None))


def _request_messages(request: LlmRequest) -> list[dict]:
    messages: list[dict] = []
    instruction = _instruction_text(getattr(request.config, "system_instruction", None))
    if instruction:
        messages.append({"role": "system", "content": instruction})

    for content in request.contents or []:
        role = "assistant" if getattr(content, "role", "user") == "model" else "user"
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        function_responses: list[dict] = []
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                text_parts.append(text)
            call = getattr(part, "function_call", None)
            if call:
                tool_calls.append({
                    "id": call.id or "",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.args or {}, separators=(",", ":")),
                    },
                })
            response = getattr(part, "function_response", None)
            if response:
                function_responses.append({
                    "role": "tool",
                    "tool_call_id": response.id or "",
                    "name": response.name or "",
                    "content": json.dumps(response.response or {}, default=str),
                })
            if getattr(part, "inline_data", None):
                raise ValueError("Groq ADK adapter currently accepts text and tool results, not inline image parts")

        if tool_calls:
            messages.append({"role": "assistant", "content": "\n".join(text_parts) or None, "tool_calls": tool_calls})
        elif text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
        messages.extend(function_responses)
    return messages


def _request_tools(request: LlmRequest) -> list[dict]:
    tools = []
    for tool in getattr(request.config, "tools", None) or []:
        for declaration in getattr(tool, "function_declarations", None) or []:
            decl = declaration.model_dump(exclude_none=True, by_alias=True)
            params = decl.get("parameters_json_schema") or decl.get("parameters") or {"type": "object", "properties": {}}
            # Groq's OpenAI-compatible API expects the JSON schema in parameters.
            tools.append({"type": "function", "function": {
                "name": decl["name"],
                **({"description": decl["description"]} if decl.get("description") else {}),
                "parameters": params,
            }})
    return tools


def _response_schema(config) -> Optional[dict]:
    schema = getattr(config, "response_json_schema", None) or getattr(config, "response_schema", None)
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    elif hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="json", exclude_none=True)
    if not isinstance(schema, dict):
        return None
    schema = dict(schema)
    name = str(schema.pop("title", "agent_response")).strip().lower()
    name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)[:64] or "agent_response"
    return {"name": name, "strict": False, "schema": schema}


class GroqADK(BaseLlm):
    """ADK model adapter with text, JSON response, and function/tool calling."""

    provider: ClassVar[str] = "groq"
    display_name: ClassVar[str] = "Groq"

    def endpoint_and_headers(self):
        key = effective_groq_key()
        if not key:
            raise RuntimeError("Groq provider is selected but no Groq API key is configured")
        return "https://api.groq.com/openai/v1/chat/completions", {"Authorization": f"Bearer {key}"}

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        endpoint, headers = self.endpoint_and_headers()
        config = llm_request.config
        payload: dict = {
            "model": self.model or settings.GROQ_MODEL,
            "messages": _request_messages(llm_request),
            "temperature": getattr(config, "temperature", None) if config else None,
        }
        # Phase 4 §4.0C (R6): bound the provider's output to the RESERVED
        # output allowance — the hold covers what the provider may generate.
        payload["max_tokens"] = int(settings.MAX_OUTPUT_RESERVE_TOKENS)
        if str(payload["model"]).startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = settings.GROQ_REASONING_EFFORT
        if payload["temperature"] is None:
            payload.pop("temperature")
        if self.provider == "azure":
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload.pop("temperature", None)
        tools = _request_tools(llm_request)
        if tools:
            payload["tools"] = tools
        response_schema = _response_schema(config) if config else None
        if response_schema and not tools:
            # Groq supports JSON-schema output mode for planner output. Groq's
            # Chat Completions API does not allow structured output with tools
            # in the same request, so tool-using turns continue in auto mode.
            payload["response_format"] = {"type": "json_schema", "json_schema": response_schema}
        elif config and getattr(config, "response_mime_type", None) == "application/json":
            payload["response_format"] = {"type": "json_object"}

        # Phase 2 (§2.11): one durable reservation per model invocation. An
        # agent runner may invoke the model many times; metering here at the
        # model boundary accounts for every underlying call. The estimate
        # covers system instruction, history, tool definitions, and schemas.
        from backend.services.budget_service import get_budget_service
        from backend.services.usage_normalizer import (
            estimate_units_from_text, normalize_openai_usage,
        )
        from backend.services.llm_context import get_run_id, get_user_id, get_org_id

        estimate = estimate_units_from_text(json.dumps(payload["messages"], default=str))
        estimate += estimate_units_from_text(json.dumps(tools, default=str)) if tools else 0
        estimate += estimate_units_from_text(json.dumps(response_schema, default=str)) if response_schema else 0
        service = get_budget_service()
        async with service.metered_call(
            user_id=get_user_id() or "platform-guest",
            estimated_input_units=estimate,
            provider=self.provider,
            model=payload["model"],
            purpose="adk_generation",
            run_id=get_run_id(),
            organization_id=get_org_id(),
        ) as call:
            await call.mark_dispatched()
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
                response = await client.post(
                    endpoint, headers=headers,
                    json=payload,
                )
            if response.status_code >= 400:
                # Avoid provider bodies: some gateways reflect request data.
                if response.status_code == 429:
                    retry_after = response.headers.get("retry-after") if response.headers else None
                    suffix = f"; retry after {retry_after}s" if retry_after and retry_after.isdigit() else ""
                    raise RuntimeError(f"{self.display_name} rate limit reached (HTTP 429{suffix})")
                raise RuntimeError(f"{self.display_name} completion failed (HTTP {response.status_code})")

            data = response.json()
            # Capture usage BEFORE parsing: invalid tool-call JSON is an
            # application failure, but the call still consumed tokens (§2.10).
            call.capture_usage(normalize_openai_usage(
                data.get("usage"), provider=self.provider, requested_model=payload["model"],
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
                    raise ValueError(f"{self.display_name} returned invalid JSON for a tool call") from exc
                parts.append(types.Part(function_call=types.FunctionCall(
                    id=tool_call.get("id"), name=fn.get("name"), args=args,
                )))
            if not parts:
                raise ValueError(f"{self.display_name} returned an empty model response")

            logger.info("ADK model call completed with %s model %s", self.display_name, payload["model"])
            yield LlmResponse(
                content=types.Content(role="model", parts=parts),
                model_version=payload["model"],
                partial=False,
            )
