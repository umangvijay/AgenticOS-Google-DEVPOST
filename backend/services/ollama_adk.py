"""Google ADK BaseLlm adapter for Ollama's OpenAI-compatible chat API
(§21.5 REAL LOCAL INFERENCE). Same structure as the Groq adapter: text,
JSON response mode, function/tool calling, streaming, budget metering at
the model boundary, and per-request credential-independent resolution
(local inference needs NO credential — that honesty is the point)."""

import json
import logging
from typing import AsyncGenerator, Optional

import httpx
from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from backend.config.settings import settings

logger = logging.getLogger(__name__)


def _instruction_text(value) -> str:
    if isinstance(value, str):
        return value
    parts = getattr(value, "parts", None) or []
    return "\n".join(p.text for p in parts if getattr(p, "text", None))


def _request_messages(request: LlmRequest) -> list[dict]:
    messages: list[dict] = []
    instruction = _instruction_text(
        getattr(request.config, "system_instruction", None))
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
                        "arguments": json.dumps(call.args or {},
                                                separators=(",", ":")),
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
        if tool_calls:
            messages.append({"role": "assistant",
                             "content": "\n".join(text_parts) or None,
                             "tool_calls": tool_calls})
        elif text_parts:
            messages.append({"role": role, "content": "\n".join(text_parts)})
        messages.extend(function_responses)
    return messages


def _request_tools(request: LlmRequest) -> list[dict]:
    tools = []
    for tool in getattr(request.config, "tools", None) or []:
        for declaration in getattr(tool, "function_declarations", None) or []:
            decl = declaration.model_dump(exclude_none=True, by_alias=True)
            params = decl.get("parameters_json_schema") or decl.get(
                "parameters") or {"type": "object", "properties": {}}
            tools.append({"type": "function", "function": {
                "name": decl["name"],
                **({"description": decl["description"]}
                   if decl.get("description") else {}),
                "parameters": params,
            }})
    return tools


def _response_schema(config) -> Optional[dict]:
    schema = getattr(config, "response_json_schema", None) or \
        getattr(config, "response_schema", None)
    if schema is None:
        return None
    if hasattr(schema, "model_json_schema"):
        schema = schema.model_json_schema()
    elif hasattr(schema, "model_dump"):
        schema = schema.model_dump(mode="json", exclude_none=True)
    if not isinstance(schema, dict):
        return None
    return {"type": "json_object"}


class OllamaADK(BaseLlm):
    """Local Ollama model adapter (OpenAI-compatible endpoint)."""

    base_url: str = ""

    def __init__(self, model: Optional[str] = None,
                 base_url: Optional[str] = None):
        super().__init__(
            model=model or settings.OLLAMA_MODEL,
            base_url=(base_url or settings.OLLAMA_BASE_URL).rstrip("/"))

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        config = llm_request.config
        payload: dict = {
            "model": self.model,
            "messages": _request_messages(llm_request),
            "temperature": getattr(config, "temperature", None) if config else None,
            "max_tokens": int(settings.MAX_OUTPUT_RESERVE_TOKENS),
        }
        if payload["temperature"] is None:
            payload.pop("temperature")
        tools = _request_tools(llm_request)
        if tools:
            payload["tools"] = tools
        response_schema = _response_schema(config) if config else None
        if response_schema and not tools:
            payload["response_format"] = response_schema

        # §2.11: ONE durable reservation per model invocation — local
        # inference is metered exactly like hosted providers (budget
        # accounting does not depend on where the model runs).
        from backend.services.budget_service import get_budget_service
        from backend.services.usage_normalizer import estimate_units_from_text
        from backend.services.llm_context import (
            get_run_id, get_user_id, get_org_id)

        estimate = estimate_units_from_text(
            json.dumps(payload["messages"], default=str))
        estimate += estimate_units_from_text(json.dumps(tools, default=str)) \
            if tools else 0
        service = get_budget_service()
        async with service.metered_call(
            user_id=get_user_id() or "platform-guest",
            estimated_input_units=estimate,
            provider="ollama",
            model=payload["model"],
            purpose="adk_generation",
            run_id=get_run_id(),
            organization_id=get_org_id(),
        ) as call:
            await call.mark_dispatched()
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        float(settings.OLLAMA_READ_TIMEOUT_S),
                        connect=10.0)) as client:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions", json=payload)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Ollama inference failed (HTTP {response.status_code}) "
                    "— is the model pulled? `docker compose --profile "
                    "local-ai up ollama` then `ollama pull <model>`")
            data = response.json()
            from backend.services.usage_normalizer import normalize_openai_usage
            call.capture_usage(normalize_openai_usage(
                data.get("usage"), provider="ollama",
                requested_model=payload["model"]))
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
                    raise ValueError(
                        "Ollama returned invalid JSON for a tool call") from exc
                parts.append(types.Part(function_call=types.FunctionCall(
                    id=tool_call.get("id"), name=fn.get("name"), args=args,
                )))
            if not parts:
                raise ValueError("Ollama returned an empty model response")
            logger.info("ADK model call completed with local model %s",
                        payload["model"])
            yield LlmResponse(
                content=types.Content(role="model", parts=parts),
                model_version=payload["model"],
                partial=False,
            )
