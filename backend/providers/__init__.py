"""Provider adapters (Phase 20 §20.7–§20.10) — the registry maps a
ProviderConfig to the right adapter without provider-specific business
logic in the orchestrator."""
from __future__ import annotations

from backend.providers.base import (
    ErrorCategory, InferenceResult, ModelCapabilities, NormalizedMessage,
    NormalizedUsage, ProviderAdapter, ProviderConfig, ProviderKind,
)
from backend.providers.bedrock import BedrockAdapter
from backend.providers.ollama import OllamaAdapter
from backend.providers.vertex_ai import VertexAIAdapter

_REGISTRY = {
    ProviderKind.VERTEX_AI: VertexAIAdapter,
    ProviderKind.BEDROCK: BedrockAdapter,
    ProviderKind.OLLAMA: OllamaAdapter,
}


def create_adapter(config: ProviderConfig) -> ProviderAdapter:
    cls = _REGISTRY.get(config.kind)
    if cls is None:
        raise ValueError(f"no adapter for provider kind {config.kind}")
    return cls(config)


__all__ = ["create_adapter", "BedrockAdapter", "OllamaAdapter",
           "VertexAIAdapter", "ProviderConfig", "ProviderKind",
           "ErrorCategory", "InferenceResult", "NormalizedMessage",
           "NormalizedUsage", "ModelCapabilities"]
