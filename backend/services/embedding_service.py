from abc import ABC, abstractmethod
from typing import List, Optional
from backend.config.settings import settings
import logging
import time

logger = logging.getLogger(__name__)

# When embedding is unavailable, fail fast instead of stalling every write.
# Measured: auth failures took ~6s per attempt under load — memory writes
# crawled at p50 5.8s with embeddings unconfigured. After the first failure
# we skip embedding attempts entirely for a cooldown window.
EMBED_FAIL_COOLDOWN_SECONDS = 60.0

_embed_unavailable_until: float = 0.0


class EmbeddingService(ABC):
    @abstractmethod
    def embed_text(self, text: str) -> List[float]:
        pass


def _embedding_configured() -> bool:
    """True when an embedding provider can plausibly work right now."""
    from backend.services.llm_context import effective_gemini_key, active_llm_provider
    if active_llm_provider() == "vertex":
        return bool(settings.GOOGLE_CLOUD_PROJECT)
    return bool(effective_gemini_key())


class GoogleCloudEmbeddingService(EmbeddingService):
    def __init__(self):
        self.client = None

    def _initialize(self):
        from google import genai

        # Initialize client according to ADK settings structure
        from backend.services.llm_context import effective_gemini_key, active_llm_provider
        if active_llm_provider() != "vertex" and effective_gemini_key():
            self.client = genai.Client(api_key=effective_gemini_key())
        else:
            self.client = genai.Client(
                vertexai=True,
                project=settings.GOOGLE_CLOUD_PROJECT,
                location=settings.GEMINI_EMBEDDING_REGION
            )

    def embed_text(self, text: str) -> List[float]:
        global _embed_unavailable_until
        # Fail fast during the cooldown — memory writes must not queue behind
        # a provider that just told us it is unavailable.
        if time.monotonic() < _embed_unavailable_until:
            raise RuntimeError("Embedding provider unavailable (cooldown after recent failure)")
        try:
            if not _embedding_configured():
                raise RuntimeError(
                    "No embedding provider configured: set a Gemini key or Vertex credentials"
                )
            if self.client is None:
                self._initialize()
            response = self.client.models.embed_content(
                model=settings.GEMINI_EMBEDDING_MODEL,
                contents=text
            )
            # Depending on genai SDK version, the structure varies.
            # Usually response.embeddings[0].values
            return response.embeddings[0].values
        except Exception as e:
            _embed_unavailable_until = time.monotonic() + EMBED_FAIL_COOLDOWN_SECONDS
            logger.warning(f"Embedding unavailable (cooldown {EMBED_FAIL_COOLDOWN_SECONDS:.0f}s): {type(e).__name__}")
            raise


async def embed_text_metered(service: EmbeddingService, text: str,
                             user_id: Optional[str] = None) -> List[float]:
    """Metered embedding (Phase 2, §2.11): one durable reservation per
    embedding request. Embedding responses carry no reliable token usage in
    the genai SDK, so settlement uses the text-length estimate, labeled
    measurement_source="estimated" — an estimate, never false precision."""
    import asyncio
    from backend.services.budget_service import get_budget_service
    from backend.services.usage_normalizer import estimate_units_from_text
    from backend.services.llm_context import get_user_id

    payer = user_id or get_user_id() or "platform-guest"
    budget = get_budget_service()
    async with budget.metered_call(
        user_id=payer,
        estimated_input_units=estimate_units_from_text(text),
        provider="gemini",
        model=settings.GEMINI_EMBEDDING_MODEL,
        purpose="embedding",
    ) as call:
        await call.mark_dispatched()
        vectors = await asyncio.to_thread(service.embed_text, text)
        # The vector itself is not billable text; exit settles the estimate.
        return vectors
