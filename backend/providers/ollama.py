"""Local inference adapter (Phase 20 §20.10) — real inference from a
downloaded Ollama model. No hosted endpoint, no canned outputs: the
model runs locally and every response comes from actual inference."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from backend.providers.base import (
    ErrorCategory, InferenceResult, ModelCapabilities, NormalizedMessage,
    NormalizedUsage, ProviderAdapter, ProviderConfig, ProviderKind,
)


def build_ollama_request(messages: List[NormalizedMessage], *,
                         tools: Optional[List[Dict[str, Any]]] = None,
                         max_tokens: int = 2048,
                         temperature: float = 0.4) -> Dict[str, Any]:
    """Ollama chat request — messages normalized, tools passed through
    for models that support function calling."""
    chat_messages = []
    for m in messages:
        role = m.role if m.role in ("user", "assistant", "system",
                                    "tool") else "user"
        if role == "tool":
            role = "user"
            content = f"Tool result: {m.content}"
        else:
            content = m.content
        entry: Dict[str, Any] = {"role": role, "content": content}
        if m.role == "assistant" and m.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": tc.get("name"),
                              "arguments": tc.get("arguments", {})}}
                for tc in m.tool_calls]
        chat_messages.append(entry)
    body: Dict[str, Any] = {
        "messages": chat_messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": t.get("description", ""),
            "parameters": t.get("parameters", {})}} for t in tools]
    return body


class OllamaAdapter(ProviderAdapter):
    kind = ProviderKind.OLLAMA
    capabilities = ModelCapabilities(
        chat=True, tools=True, streaming=True)

    def __init__(self, config: ProviderConfig):
        self.config = config
        problems = config.validate()
        if problems:
            raise ValueError("; ".join(problems))
        self.config.base_url = (self.config.base_url
                                or "http://localhost:11434").rstrip("/")

    def validate_credentials(self) -> Dict[str, Any]:
        """Local inference needs no credentials — check the SERVICE is
        reachable and the model is downloaded."""
        try:
            res = httpx.get(f"{self.config.base_url}/api/tags", timeout=5)
            if res.status_code != 200:
                return {"status": "unavailable",
                        "detail": f"ollama returned {res.status_code}"}
            models = [m.get("name") for m in res.json().get("models", [])]
            model_present = any(
                (m or "").startswith(self.config.model_id) for m in models)
            return {"status": "accepted",
                    "mode": "local",
                    "model_downloaded": model_present,
                    "models": models[:10]}
        except httpx.ConnectError:
            return {"status": "unavailable",
                    "detail": ("Ollama is not reachable at "
                               f"{self.config.base_url} — start it and "
                               "pull the model: `ollama pull "
                               f"{self.config.model_id}`")}

    async def generate(self, messages: List[NormalizedMessage], *,
                       tools: Optional[List[Dict[str, Any]]] = None,
                       max_tokens: int = 2048,
                       temperature: float = 0.4) -> InferenceResult:
        body = build_ollama_request(messages, tools=tools,
                                    max_tokens=max_tokens,
                                    temperature=temperature)
        body["model"] = self.config.model_id
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                res = await client.post(f"{self.config.base_url}/api/chat",
                                        json=body)
        except httpx.ConnectError as exc:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="ollama",
                                   error_category=ErrorCategory.NETWORK,
                                   error_detail=str(exc)[:200])
        if res.status_code != 200:
            return InferenceResult(
                text="", usage=NormalizedUsage(), provider="ollama",
                error_category=normalize_ollama_error(res.status_code),
                error_detail=res.text[:300])
        data = res.json()
        message = data.get("message", {}) or {}
        text = message.get("content", "")
        tool_calls = []
        for tc in (message.get("tool_calls") or []):
            fn = tc.get("function", {})
            tool_calls.append({"name": fn.get("name"),
                               "arguments": fn.get("arguments", {})})
        usage_data = data.get("eval_count") is not None
        usage = NormalizedUsage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            total_tokens=(int(data.get("prompt_eval_count") or 0)
                          + int(data.get("eval_count") or 0)),
            known=usage_data)
        return InferenceResult(text=text, usage=usage, tool_calls=tool_calls,
                               finish_reason="stop",
                               model_id=self.config.model_id,
                               provider="ollama")

    async def generate_stream(self, messages: List[NormalizedMessage], *,
                              max_tokens: int = 2048,
                              temperature: float = 0.4):
        body = build_ollama_request(messages, max_tokens=max_tokens,
                                    temperature=temperature)
        body["model"] = self.config.model_id
        body["stream"] = True
        import json as _json
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream(
                    "POST", f"{self.config.base_url}/api/chat",
                    json=body) as res:
                if res.status_code != 200:
                    text = (await res.aread()).decode()[:300]
                    yield InferenceResult(
                        text="", usage=NormalizedUsage(),
                        provider="ollama",
                        error_category=normalize_ollama_error(
                            res.status_code), error_detail=text)
                    return
                async for line in res.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = _json.loads(line)
                    if chunk.get("done"):
                        yield InferenceResult(
                            text="",
                            usage=NormalizedUsage(
                                input_tokens=int(chunk.get(
                                    "prompt_eval_count") or 0),
                                output_tokens=int(chunk.get(
                                    "eval_count") or 0),
                                known=True),
                            provider="ollama")
                        return
                    content = (chunk.get("message") or {}).get("content", "")
                    if content:
                        yield InferenceResult(text=content,
                                              usage=NormalizedUsage(),
                                              provider="ollama")


def normalize_ollama_error(status: int) -> ErrorCategory:
    if status == 404:
        return ErrorCategory.NOT_FOUND
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status >= 500:
        return ErrorCategory.SERVER
    if status == 400:
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN
