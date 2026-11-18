"""Model provider adapters (Phase 20 §20.7/§20.8) — one normalized
interface over Vertex AI, Amazon Bedrock, local inference (Ollama), and
the existing Gemini developer-API provider.

Normalization contract: messages in/out, tool calls, usage accounting,
error categories, capability declarations, credential REFERENCES (never
raw secrets in payloads). The same compatible agent workflow runs
through any adapter without provider-specific business logic in the
orchestrator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProviderKind(str, Enum):
    GEMINI_DEV = "gemini_dev"           # developer API (existing)
    VERTEX_AI = "vertex_ai"             # standard ADC or express API key
    BEDROCK = "bedrock"                 # Converse API
    OLLAMA = "ollama"                   # local inference


class ErrorCategory(str, Enum):
    AUTH = "auth"                       # invalid/expired credentials
    NOT_FOUND = "model_not_found"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    CANCELLED = "cancelled"
    NETWORK = "network"
    SERVER = "server_error"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


@dataclass
class NormalizedMessage:
    role: str                            # user | assistant | system | tool
    content: str
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


@dataclass
class NormalizedUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    known: bool = False


@dataclass
class ModelCapabilities:
    chat: bool = True
    tools: bool = False
    streaming: bool = False
    images: bool = False
    embeddings: bool = False
    structured_output: bool = False


@dataclass
class ProviderConfig:
    kind: ProviderKind
    model_id: str
    # Vertex AI standard: project + location + ADC.
    project: Optional[str] = None
    location: Optional[str] = None
    # Vertex express mode: API key (different endpoint form).
    api_key: Optional[str] = None
    # Bedrock: region + optional bearer token (AWS_BEARER_TOKEN_BEDROCK)
    # or the SDK credential chain.
    region: Optional[str] = None
    bearer_token: Optional[str] = None
    # Ollama: local base URL.
    base_url: Optional[str] = None
    credential_reference: Optional[str] = None   # a SECRET REFERENCE, never the secret

    def validate(self) -> List[str]:
        problems = []
        if self.kind == ProviderKind.VERTEX_AI:
            if not self.api_key and not (self.project and self.location):
                problems.append(
                    "Vertex AI needs either an express-mode API key or "
                    "project+location with Application Default Credentials "
                    "— the two modes are NOT interchangeable")
        if self.kind == ProviderKind.BEDROCK and not self.region:
            problems.append("Bedrock requires a region")
        if self.kind == ProviderKind.OLLAMA and not self.base_url:
            problems.append("Ollama requires a base URL")
        if not self.model_id:
            problems.append("model_id is required")
        return problems


@dataclass
class InferenceResult:
    text: str
    usage: NormalizedUsage
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    model_id: str = ""
    provider: str = ""
    error_category: Optional[ErrorCategory] = None
    error_detail: Optional[str] = None


class ProviderAdapter(ABC):
    """The normalized inference contract. Implementations translate to
    the provider SDK; the orchestrator never branches on provider."""

    kind: ProviderKind
    capabilities: ModelCapabilities

    @abstractmethod
    def validate_credentials(self) -> Dict[str, Any]:
        """Check credential acceptance WITHOUT running inference.
        Returns {status: accepted|denied|unavailable, detail}."""

    @abstractmethod
    async def generate(self, messages: List[NormalizedMessage], *,
                       tools: Optional[List[Dict[str, Any]]] = None,
                       max_tokens: int = 2048,
                       temperature: float = 0.4) -> InferenceResult:
        """One normalized inference call. Errors surface with an
        ErrorCategory — never a raw exception type."""

    @abstractmethod
    async def generate_stream(self, messages: List[NormalizedMessage], *,
                              max_tokens: int = 2048,
                              temperature: float = 0.4):
        """Normalized streaming: yields text deltas, final usage last."""

    def model_capabilities(self, model_id: Optional[str] = None) -> ModelCapabilities:
        return self.capabilities
