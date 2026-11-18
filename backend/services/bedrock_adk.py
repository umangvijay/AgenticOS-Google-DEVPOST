"""Google ADK BaseLlm adapter for Amazon Bedrock's Converse API
(§21.5). Auth modes: `AWS_BEARER_TOKEN_BEDROCK` bearer header OR the
boto3 default credential chain — resolved PER CALL from context-local
state so concurrent users never exchange credentials (§21.6). Text,
tool calling (toolUse/toolResult blocks), budget metering, and honest
provider errors."""

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Optional

import httpx
from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from backend.config.settings import settings
from backend.services.llm_context import effective_bedrock_credentials

logger = logging.getLogger(__name__)


def _instruction_text(value) -> str:
    if isinstance(value, str):
        return value
    parts = getattr(value, "parts", None) or []
    return "\n".join(p.text for p in parts if getattr(p, "text", None))


def _converse_messages(request: LlmRequest) -> tuple[list[dict], list[dict]]:
    """LlmRequest → (Converse messages, system list). Tool calls become
    toolUse blocks; tool results become toolResult blocks."""
    system_text = _instruction_text(
        getattr(request.config, "system_instruction", None))
    system = [{"text": system_text}] if system_text else []
    messages: list[dict] = []
    for content in request.contents or []:
        role = "assistant" if getattr(content, "role", "user") == "model" else "user"
        blocks: list[dict] = []
        pending_results: list[dict] = []
        for part in getattr(content, "parts", None) or []:
            text = getattr(part, "text", None)
            if text:
                blocks.append({"text": text})
            call = getattr(part, "function_call", None)
            if call:
                blocks.append({"toolUse": {
                    "toolUseId": call.id or f"tu_{uuid.uuid4().hex[:8]}",
                    "name": call.name,
                    "input": call.args or {},
                }})
            response = getattr(part, "function_response", None)
            if response:
                pending_results.append({"toolResult": {
                    "toolUseId": response.id or "",
                    "content": [{"json": response.response or {}}],
                }})
        if blocks:
            messages.append({"role": role, "content": blocks})
        for block in pending_results:
            messages.append({"role": "user", "content": [block]})
    return messages, system


def _converse_tools(request: LlmRequest) -> Optional[dict]:
    specs = []
    for tool in getattr(request.config, "tools", None) or []:
        for declaration in getattr(tool, "function_declarations", None) or []:
            decl = declaration.model_dump(exclude_none=True, by_alias=True)
            params = decl.get("parameters_json_schema") or decl.get(
                "parameters") or {"type": "object", "properties": {}}
            specs.append({"toolSpec": {
                "name": decl["name"],
                **({"description": decl["description"]}
                   if decl.get("description") else {}),
                "inputSchema": {"json": params},
            }})
    if not specs:
        return None
    return {"tools": specs}


def _parse_converse(data: dict, model_id: str) -> LlmResponse:
    out = data.get("output") or {}
    message = out.get("message") or {}
    parts = []
    text = "".join(b.get("text", "") for b in message.get("content") or []
                   if "text" in b)
    if text:
        parts.append(types.Part(text=text))
    for block in message.get("content") or []:
        use = block.get("toolUse")
        if use:
            parts.append(types.Part(function_call=types.FunctionCall(
                id=use.get("toolUseId"), name=use.get("name"),
                args=use.get("input") or {},
            )))
    if not parts:
        raise ValueError("Bedrock returned an empty model response")
    return LlmResponse(
        content=types.Content(role="model", parts=parts),
        model_version=model_id, partial=False)


class BedrockADK(BaseLlm):
    region: str = "us-east-1"

    def __init__(self, model: Optional[str] = None):
        super().__init__(model=model or settings.BEDROCK_MODEL_ID,
                         region=settings.AWS_REGION)

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=True)

    def _auth_headers(self, bearer: str) -> dict:
        return {"Authorization": f"Bearer {bearer}"}

    async def _post_signed(self, payload: dict, bearer: Optional[str]):
        """One HTTP call to Converse. Bearer mode uses the token directly;
        chain mode signs with SigV4 via botocore in an executor."""
        url = (f"https://bedrock-runtime.{self.region}.amazonaws.com"
               f"/model/{self.model}/converse")
        if bearer:
            headers = self._auth_headers(bearer)
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                return await client.post(url, json=payload, headers=headers)
        # boto3 credential chain (runs the SYNC SDK in an executor).
        import botocore.session
        import botocore.auth
        from botocore.awsrequest import AWSRequest
        # Sign exactly the bytes that will be transmitted. httpx's json=
        # encoder uses different separators than json.dumps by default.
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        def _sign():
            session = botocore.session.get_session()
            credentials = session.get_credentials()
            if credentials is None:
                raise ValueError("Bedrock credentials unavailable in the configured SDK chain")
            creds = credentials.get_frozen_credentials()
            request = AWSRequest(method="POST", url=url,
                                 data=body)
            request.headers["Content-Type"] = "application/json"
            signer = botocore.auth.SigV4Auth(creds, "bedrock", self.region)
            signer.add_auth(request)
            return dict(request.headers)

        headers = await asyncio.to_thread(_sign)
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            return await client.post(url, content=body, headers=headers)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        bearer, _region = effective_bedrock_credentials()
        messages, system = _converse_messages(llm_request)
        payload: dict = {"modelId": self.model, "messages": messages}
        if system:
            payload["system"] = system
        tools = _converse_tools(llm_request)
        if tools:
            payload["toolConfig"] = tools

        from backend.services.budget_service import get_budget_service
        from backend.services.usage_normalizer import estimate_units_from_text
        from backend.services.llm_context import (
            get_run_id, get_user_id, get_org_id)

        estimate = estimate_units_from_text(json.dumps(messages, default=str))
        estimate += estimate_units_from_text(json.dumps(tools, default=str)) \
            if tools else 0
        service = get_budget_service()
        async with service.metered_call(
            user_id=get_user_id() or "platform-guest",
            estimated_input_units=estimate,
            provider="bedrock",
            model=self.model,
            purpose="adk_generation",
            run_id=get_run_id(),
            organization_id=get_org_id(),
        ) as call:
            await call.mark_dispatched()
            response = await self._post_signed(payload, bearer)
            if response.status_code >= 400:
                # No provider body reflection (§ auth hygiene).
                if response.status_code == 403:
                    raise RuntimeError(
                        "Bedrock rejected the credential (HTTP 403) — "
                        "attach a valid key for this integration")
                if response.status_code == 429:
                    raise RuntimeError(
                        "Bedrock rate limit reached (HTTP 429)")
                raise RuntimeError(
                    f"Bedrock inference failed (HTTP {response.status_code})")
            data = response.json()
            usage = data.get("usage") or {}
            call.capture_usage({
                "prompt_tokens": usage.get("inputTokens", 0),
                "completion_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("totalTokens",
                                          usage.get("inputTokens", 0)
                                          + usage.get("outputTokens", 0)),
            })
            parsed = _parse_converse(data, self.model)
            for part in parsed.content.parts:
                if part.text:
                    call.capture_response_text(part.text)
            logger.info("ADK model call completed with Bedrock model %s",
                        self.model)
            yield parsed
