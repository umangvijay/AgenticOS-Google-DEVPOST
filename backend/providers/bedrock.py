"""Amazon Bedrock adapter (Phase 20 §20.8) — the Converse API with:

- region + configured model/inference-profile identifiers,
- API-key bearer auth (AWS_BEARER_TOKEN_BEDROCK) OR the SDK credential
  chain (temporary credentials with expiry handled by the SDK),
- normalized usage and error categories.

Request shaping is tested with controlled fakes; live credentials are
only needed for the live-provider gate."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.providers.base import (
    ErrorCategory, InferenceResult, ModelCapabilities, NormalizedMessage,
    NormalizedUsage, ProviderAdapter, ProviderConfig, ProviderKind,
)


def normalize_bedrock_error(status: int, body: str) -> ErrorCategory:
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


def build_converse_request(messages: List[NormalizedMessage], *,
                           tools: Optional[List[Dict[str, Any]]] = None,
                           max_tokens: int = 2048,
                           temperature: float = 0.4) -> Dict[str, Any]:
    """The Bedrock Converse request body — normalized messages in,
    tool specs translated to Bedrock's toolConfig shape."""
    converse_messages = []
    system_parts: List[Dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            system_parts.append({"text": m.content})
            continue
        if m.role == "tool":
            converse_messages.append({
                "role": "user",
                "content": [{"toolResult": {
                    "toolUseId": m.tool_call_id or "unknown",
                    "content": [{"json": {"result": m.content}}]}}]})
            continue
        content_blocks: List[Dict[str, Any]] = [{"text": m.content}]
        if m.role == "assistant" and m.tool_calls:
            content_blocks = []
            if m.content:
                content_blocks.append({"text": m.content})
            for tc in m.tool_calls:
                content_blocks.append({"toolUse": {
                    "toolUseId": tc.get("id", ""),
                    "name": tc.get("name"),
                    "input": tc.get("arguments", {})}})
        converse_messages.append({
            "role": "assistant" if m.role == "assistant" else "user",
            "content": content_blocks})
    body: Dict[str, Any] = {
        "messages": converse_messages,
        "inferenceConfig": {"maxTokens": max_tokens,
                            "temperature": temperature},
    }
    if system_parts:
        body["system"] = system_parts
    if tools:
        body["toolConfig"] = {"tools": [
            {"toolSpec": {"name": t.get("name"),
                          "description": t.get("description", ""),
                          "inputSchema": {"json": t.get("parameters", {})}}
             } for t in tools]}
    return body


def parse_converse_response(data: Dict[str, Any]) -> InferenceResult:
    text = ""
    tool_calls: List[Dict[str, Any]] = []
    stop = ""
    for out in (data.get("output") or []):
        message = out.get("message", {})
        for block in (message.get("content") or []):
            if "text" in block:
                text += block["text"]
            if "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append({"id": tu.get("toolUseId"),
                                   "name": tu.get("name"),
                                   "arguments": tu.get("input", {})})
        stop = out.get("stopReason") or stop
    usage_data = data.get("usage", {}) or {}
    usage = NormalizedUsage(
        input_tokens=int(usage_data.get("inputTokens") or 0),
        output_tokens=int(usage_data.get("outputTokens") or 0),
        total_tokens=int(usage_data.get("totalTokens") or 0),
        known=bool(usage_data))
    return InferenceResult(text=text, usage=usage, tool_calls=tool_calls,
                           finish_reason=(stop or "end_turn").lower(),
                           provider="bedrock")


class BedrockAdapter(ProviderAdapter):
    kind = ProviderKind.BEDROCK
    capabilities = ModelCapabilities(
        chat=True, tools=True, streaming=True, structured_output=True)

    def __init__(self, config: ProviderConfig):
        self.config = config
        problems = config.validate()
        if problems:
            raise ValueError("; ".join(problems))

    def _endpoint(self) -> str:
        return (f"https://bedrock-runtime.{self.config.region}.amazonaws.com/"
                f"model/{self.config.model_id}/converse")

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # API-key bearer auth (AWS_BEARER_TOKEN_BEDROCK) where supported;
        # otherwise the SDK credential chain supplies SigV4 at call time.
        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        return headers

    def validate_credentials(self) -> Dict[str, Any]:
        if self.config.bearer_token:
            if not self.config.bearer_token.strip():
                return {"status": "denied",
                        "detail": "empty Bedrock bearer token"}
            return {"status": "accepted", "mode": "bearer_api_key"}
        try:
            import boto3
            session = boto3.session.Session()
            credentials = session.get_credentials()
            if credentials is None:
                return {"status": "unavailable",
                        "detail": "no AWS credential chain"}
            frozen = credentials.get_frozen_credentials()
            expires = getattr(frozen, "expiry_time", None)
            return {"status": "accepted", "mode": "credential_chain",
                    "region": self.config.region,
                    "expires": str(expires) if expires else None}
        except Exception as exc:
            return {"status": "unavailable",
                    "detail": f"AWS SDK credential chain failed: "
                              f"{str(exc)[:200]}"}

    async def generate(self, messages: List[NormalizedMessage], *,
                       tools: Optional[List[Dict[str, Any]]] = None,
                       max_tokens: int = 2048,
                       temperature: float = 0.4) -> InferenceResult:
        import httpx
        if self.config.bearer_token:
            body = build_converse_request(messages, tools=tools,
                                          max_tokens=max_tokens,
                                          temperature=temperature)
            endpoint = self._endpoint()
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    res = await client.post(endpoint, json=body,
                                            headers=self._headers())
            except httpx.ConnectError as exc:
                return InferenceResult(text="", usage=NormalizedUsage(),
                                       provider="bedrock",
                                       error_category=ErrorCategory.NETWORK,
                                       error_detail=str(exc)[:200])
            if res.status_code != 200:
                return InferenceResult(
                    text="", usage=NormalizedUsage(), provider="bedrock",
                    error_category=normalize_bedrock_error(
                        res.status_code, res.text[:200]),
                    error_detail=res.text[:300])
            return parse_converse_response(res.json())
        # SDK credential chain: use boto3's Converse call (SigV4 signed,
        # temporary credentials refreshed by the SDK).
        import asyncio
        try:
            import boto3
        except ImportError:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="bedrock",
                                   error_category=ErrorCategory.AUTH,
                                   error_detail="boto3 not installed")
        client = boto3.client("bedrock-runtime",
                              region_name=self.config.region)
        body = build_converse_request(messages, tools=tools,
                                      max_tokens=max_tokens,
                                      temperature=temperature)
        kwargs: Dict[str, Any] = {
            "modelId": self.config.model_id,
            "messages": body["messages"],
            "inferenceConfig": body["inferenceConfig"]}
        if "system" in body:
            kwargs["system"] = body["system"]
        if "toolConfig" in body:
            kwargs["toolConfig"] = body["toolConfig"]
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: client.converse(**kwargs))
        except client.exceptions.AccessDeniedException:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="bedrock",
                                   error_category=ErrorCategory.AUTH,
                                   error_detail="access denied")
        except client.exceptions.ThrottlingException:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="bedrock",
                                   error_category=ErrorCategory.RATE_LIMIT,
                                   error_detail="throttled")
        except client.exceptions.ResourceNotFoundException:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="bedrock",
                                   error_category=ErrorCategory.NOT_FOUND,
                                   error_detail="model not found")
        except Exception as exc:
            return InferenceResult(text="", usage=NormalizedUsage(),
                                   provider="bedrock",
                                   error_category=ErrorCategory.UNKNOWN,
                                   error_detail=str(exc)[:300])
        result = parse_converse_response(response)
        result.model_id = self.config.model_id
        return result

    async def generate_stream(self, messages: List[NormalizedMessage], *,
                              max_tokens: int = 2048,
                              temperature: float = 0.4):
        """ConverseStream via the SDK credential chain; yields text
        deltas, final usage last."""
        import asyncio
        import boto3
        client = boto3.client("bedrock-runtime",
                              region_name=self.config.region)
        body = build_converse_request(messages, max_tokens=max_tokens,
                                      temperature=temperature)
        kwargs: Dict[str, Any] = {
            "modelId": self.config.model_id,
            "messages": body["messages"],
            "inferenceConfig": body["inferenceConfig"]}
        if "system" in body:
            kwargs["system"] = body["system"]
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None, lambda: client.converse_stream(**kwargs))
        stream = response.get("stream")
        text_parts: List[str] = []
        usage: Optional[NormalizedUsage] = None
        for event in (stream or []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                if "text" in delta:
                    text_parts.append(delta["text"])
                    yield InferenceResult(text=delta["text"],
                                          usage=NormalizedUsage(),
                                          provider="bedrock")
            if "metadata" in event:
                u = event["metadata"].get("usage", {}) or {}
                usage = NormalizedUsage(
                    input_tokens=int(u.get("inputTokens") or 0),
                    output_tokens=int(u.get("outputTokens") or 0),
                    total_tokens=int(u.get("totalTokens") or 0),
                    known=True)
        if usage is not None:
            yield InferenceResult(text="", usage=usage, provider="bedrock")
