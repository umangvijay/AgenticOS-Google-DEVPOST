"""Azure OpenAI v1 chat completions using an operator-selected deployment."""
from typing import ClassVar
from urllib.parse import urlsplit

from backend.config.settings import settings
from backend.services.groq_adk import GroqADK
from backend.services.llm_context import effective_azure_key


def azure_endpoint():
    endpoint = urlsplit(settings.AZURE_OPENAI_ENDPOINT)
    host = endpoint.hostname or ""
    if (endpoint.scheme != "https" or not host.endswith(".openai.azure.com")
            or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise ValueError("Azure OpenAI requires a configured HTTPS Azure resource endpoint")
    return f"https://{host}/openai/v1"


class AzureADK(GroqADK):
    provider: ClassVar[str] = "azure"
    display_name: ClassVar[str] = "Azure OpenAI"

    def endpoint_and_headers(self):
        key = effective_azure_key()
        if not key:
            raise ValueError("Azure OpenAI API key is unavailable")
        return azure_endpoint() + "/chat/completions", {"api-key": key}
