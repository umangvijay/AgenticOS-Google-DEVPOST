"""Vertex AI adapter (Phase 20 §20.8) — TWO DISTINCT auth modes:

- Standard: project + location + Application Default Credentials (the
  google.auth chain) → the `-aiplatform.googleapis.com` endpoint.
- Express mode: API key → the documented express endpoint form with
  `?key=` auth. These modes are NOT interchangeable strings.

Real inference via the installed google-genai client; request shaping
and error normalization are tested with controlled fakes so live
credentials are only needed for the live-provider gate.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.providers.base import (
    ErrorCategory, InferenceResult, ModelCapabilities, NormalizedMessage,
    NormalizedUsage, ProviderAdapter, ProviderConfig, ProviderKind,
)

EXPRESS_ENDPOINT = (
    "https://aiplatform.googleapis.com/v1/publishers/google/models/"
    "{model}:generateContent")


def normalize_vertex_error(status: int, body: str) -> ErrorCategory:
    if status in (401, 403):
        return ErrorCategory.AUTH
    if status == 404:
        return ErrorCategory.NOT_FOUND
    if status == 429:
        return ErrorCategory.RATE_LIMIT
    if status >= 500:
        return ErrorCategory.SERVER
    if status == 400:
        return ErrorCategory.INVALID_REQUEST
    return ErrorCategory.UNKNOWN


def build_generate_request(messages: List[NormalizedMessage], *,
                           tools: Optional[List[Dict[str, Any]]] = None,
                           max_tokens: int = 2048,
                           temperature: float = 0.4) -> Dict[str, Any]:
    """The Vertex generateContent request body — shared by both auth
    modes so request shaping is testable without credentials."""
    contents = []
    system_parts = []
    for m in messages:
        if m.role == "system":
            system_parts.append({"text": m.content})
            continue
        role = "user" if m.role in ("user", "tool") else "model"
        parts: List[Dict[str, Any]] = [{"text": m.content}]
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                parts.append({"functionCall": {
                    "name": tc.get("name"),
                    "args": tc.get("arguments", {})}})
        if m.role == "tool" and m.tool_call_id:
            parts = [{"functionResponse": {
                "name": m.tool_call_id,
                "response": {"result": m.content}}}]
        contents.append({"role": role, "parts": parts})
    body: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens,
                             "temperature": temperature},
    }
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t.get("name"),
             "description": t.get("description", ""),
             "parameters": t.get("parameters", {})} for t in tools]}]
    return body


class VertexAIAdapter(ProviderAdapter):
    kind = ProviderKind.VERTEX_AI
    capabilities = ModelCapabilities(
        chat=True, tools=True, streaming=True, images=True,
        structured_output=True)

    def __init__(self, config: ProviderConfig):
        self.config = config
        problems = config.validate()
        if problems:
            raise ValueError("; ".join(problems))

    def _endpoint(self) -> str:
        if self.config.api_key:
            # Express mode: API-key endpoint form.
            return EXPRESS_ENDPOINT.format(
                model=self.config.model_id) + f"?key={self.config.api_key}"
        # Standard mode: project/location endpoint (ADC supplies auth
        # headers via the installed client at call time).
        host = ("aiplatform.googleapis.com" if self.config.location == "global"
                else f"{self.config.location}-aiplatform.googleapis.com")
        return (f"https://{host}/"
                f"v1/projects/{self.config.project}/locations/"
                f"{self.config.location}/publishers/google/models/"
                f"{self.config.model_id}:generateContent")

    def validate_credentials(self) -> Dict[str, Any]:
        """Credential acceptance WITHOUT inference: express mode checks
        the key exists; standard mode checks the ADC chain resolves."""
        try:
            if self.config.api_key:
                if not self.config.api_key.strip():
                    return {"status": "denied",
                            "detail": "empty express-mode API key"}
                return {"status": "accepted",
                        "mode": "express_api_key"}
            import google.auth
            credentials, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            if credentials is None:
                return {"status": "unavailable",
                        "detail": "no Application Default Credentials"}
            return {"status": "accepted", "mode": "adc",
                    "project": project or self.config.project}
        except Exception as exc:
            return {"status": "unavailable",
                    "detail": f"ADC not resolvable: {str(exc)[:200]}"}

    async def generate(self, messages: List[NormalizedMessage], *,
                       tools: Optional[List[Dict[str, Any]]] = None,
                       max_tokens: int = 2048,
                       temperature: float = 0.4) -> InferenceResult:
        import httpx
        body = build_generate_request(messages, tools=tools,
                                      max_tokens=max_tokens,
                                      temperature=temperature)
        endpoint = self._endpoint()
        headers = {"Content-Type": "application/json"}
        if not self.config.api_key:
            # Standard mode: ADC bearer token.
            import google.auth
            import google.auth.transport.requests as _tr
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(_tr.Request())
            headers["Authorization"] = f"Bearer {credentials.token}"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                res = await client.post(endpoint, json=body, headers=headers)
        except httpx.ConnectError as exc:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="vertex_ai",
                                   error_category=ErrorCategory.NETWORK,
                                   error_detail=str(exc)[:200])
        if res.status_code != 200:
            return InferenceResult(
                text="", usage=NormalizedUsage(), provider="vertex_ai",
                error_category=normalize_vertex_error(
                    res.status_code, res.text[:200]),
                error_detail=res.text[:300])
        data = res.json()
        text = ""
        tool_calls: List[Dict[str, Any]] = []
        for cand in data.get("candidates", []):
            for part in (cand.get("content", {}).get("parts", []) or []):
                if "text" in part:
                    text += part["text"]
                if "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append({"name": fc.get("name"),
                                       "arguments": fc.get("args", {})})
        usage_data = data.get("usageMetadata", {}) or {}
        usage = NormalizedUsage(
            input_tokens=int(usage_data.get("promptTokenCount") or 0),
            output_tokens=int(usage_data.get("candidatesTokenCount") or 0),
            total_tokens=int(usage_data.get("totalTokenCount") or 0),
            known=bool(usage_data))
        finish = (data.get("candidates") or [{}])[0].get(
            "finishReason", "STOP")
        return InferenceResult(text=text, usage=usage, tool_calls=tool_calls,
                               finish_reason=finish.lower(),
                               model_id=self.config.model_id,
                               provider="vertex_ai")

    async def generate_stream(self, messages: List[NormalizedMessage], *,
                              max_tokens: int = 2048,
                              temperature: float = 0.4):
        """Stream via streamGenerateContent with SSE; yields text deltas,
        final usage last. The endpoint/auth match generate()."""
        import httpx
        body = build_generate_request(messages, max_tokens=max_tokens,
                                      temperature=temperature)
        body["generationConfig"]["candidateCount"] = 1
        endpoint = self._endpoint().replace(":generateContent", ":streamGenerateContent")
        endpoint += ("&" if "?" in endpoint else "?") + "alt=sse"
        headers = {"Content-Type": "application/json"}
        if not self.config.api_key:
            import google.auth
            import google.auth.transport.requests as _tr
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
            credentials.refresh(_tr.Request())
            headers["Authorization"] = f"Bearer {credentials.token}"
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", endpoint, json=body,
                                     headers=headers) as res:
                if res.status_code != 200:
                    text = (await res.aread()).decode()[:300]
                    yield InferenceResult(
                        text="", usage=NormalizedUsage(),
                        provider="vertex_ai",
                        error_category=normalize_vertex_error(
                            res.status_code, text),
                        error_detail=text)
                    return
                import json as _json
                async for line in res.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    chunk = _json.loads(payload)
                    usage_data = chunk.get("usageMetadata") or {}
                    for cand in chunk.get("candidates", []):
                        for part in (cand.get("content", {}).get(
                                "parts", []) or []):
                            if "text" in part:
                                yield InferenceResult(
                                    text=part["text"],
                                    usage=NormalizedUsage(),
                                    provider="vertex_ai")
                    if usage_data:
                        yield InferenceResult(
                            text="",
                            usage=NormalizedUsage(
                                input_tokens=int(usage_data.get(
                                    "promptTokenCount") or 0),
                                output_tokens=int(usage_data.get(
                                    "candidatesTokenCount") or 0),
                                total_tokens=int(usage_data.get(
                                    "totalTokenCount") or 0),
                                known=True),
                            provider="vertex_ai")
