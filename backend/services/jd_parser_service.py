import logging
from backend.models.resume import JobDescription
from backend.config.settings import settings

logger = logging.getLogger(__name__)

class JDParserService:
    def __init__(self):
        from backend.services.gemini_client import _client
        self.client = _client()

    async def parse_job_description(self, raw_text: str) -> JobDescription:
        """Parses a raw JD string into a structured JobDescription Pydantic
        model. Phase 2 (§2.11): routed through the shared metered client so
        every attempt holds a durable budget reservation."""
        prompt = f"""
        Extract the structured details from the following job description.
        If a field is missing or ambiguous, omit it or leave it blank.
        Extract technologies and tools into required_skills or preferred_skills as appropriate.
        Extract any educational requirements.
        Extract key responsibilities as brief bullet points.
        
        Job Description:
        {raw_text}
        """
        from backend.services.gemini_client import generate_json
        try:
            payload = await generate_json(prompt, schema=JobDescription)
            if isinstance(payload, JobDescription):
                return payload
            return JobDescription.model_validate(payload)
        except Exception as e:
            logger.error(f"Failed to parse JD: {e}")
            raise
