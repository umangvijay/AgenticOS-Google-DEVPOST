"""Documented Gemini IDs, separate from deployment enablement and live access.

Sources checked 2026-11-19. No network calls on the settings/request path.
Publisher discovery is NOT proof that a project's identity can run a model;
scripts/gcp/verify_vertex_models.py records that evidence separately.
"""
from dataclasses import asdict, dataclass
from datetime import date

DOCS = "https://docs.cloud.google.com/gemini-enterprise-agent-platform/models"
CHECKED = "2026-11-19"


@dataclass(frozen=True)
class GeminiModel:
    id: str
    name: str
    family: str
    documentation: str
    lifecycle: str = "ga"
    retirement_date: str = ""
    workflow_supported: bool = True
    note: str = ""


MODELS = (
    GeminiModel("gemini-3.8-flash", "Gemini 3.8 Flash", "Flash", f"{DOCS}/gemini/3-8-flash"),
    GeminiModel("gemini-4-hydrogen", "Gemini 4 Hydrogen", "Gemini 4", f"{DOCS}/gemini/4-hydrogen"),
    GeminiModel("gemini-4-neon", "Gemini 4 Neon", "Gemini 4", f"{DOCS}/gemini/4-neon",
                note="Restricted to verified enterprise customers in supported locations."),
    GeminiModel("gemini-4-argon", "Gemini 4 Argon", "Gemini 4", f"{DOCS}/gemini/4-argon"),
    GeminiModel("gemini-4-carbon", "Gemini 4 Carbon", "Gemini 4", f"{DOCS}/gemini/4-carbon"),
    GeminiModel("gemini-3.7-flash", "Gemini 3.7 Flash", "Flash", f"{DOCS}/gemini/3-7-flash",
                retirement_date="2027-01-28"),
    GeminiModel("gemini-3.5-flash", "Gemini 3.5 Flash", "Flash", f"{DOCS}/gemini/3-5-flash"),
    GeminiModel("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "Flash-Lite", f"{DOCS}/gemini/3-5-flash-lite"),
    GeminiModel("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite", "Flash-Lite", f"{DOCS}/gemini/3-1-flash-lite"),
    GeminiModel("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", "Pro", f"{DOCS}/gemini/3-1-pro", "preview"),
    GeminiModel("gemini-3.1-pro-preview-customtools", "Gemini 3.1 Pro Custom Tools Preview", "Pro",
                f"{DOCS}/gemini/3-1-pro", "preview", note="Optimized for custom tools; preview limitations apply."),
    GeminiModel("gemini-3-flash-preview", "Gemini 3 Flash Preview", "Flash", f"{DOCS}/gemini/3-flash", "preview"),
    GeminiModel("gemini-3.6-flash", "Gemini 3.6 Flash", "Retired", f"{DOCS}/model-versions",
                retirement_date="2026-11-19"),
    # Different APIs/output types must never enter a text/tool workflow merely
    # because their name contains Gemini or Flash.
    GeminiModel("gemini-3.8-live", "Gemini 3.8 Live", "Specialized", f"{DOCS}/google-models",
                workflow_supported=False, note="Requires the Live audio session API."),
    GeminiModel("gemini-nano-banana-2.1", "Gemini Nano Banana 2.1", "Specialized", f"{DOCS}/google-models",
                workflow_supported=False, note="Image generation is not supported by this workflow selector."),
    GeminiModel("gemini-omni-1.2-flash", "Gemini Omni 1.2 Flash", "Specialized", f"{DOCS}/google-models",
                workflow_supported=False, note="Video generation requires a separate execution path."),
    GeminiModel("gemini-embedding-2", "Gemini Embedding 2", "Specialized", f"{DOCS}/model-versions",
                workflow_supported=False, note="Embeddings cannot plan or execute workflows."),
    GeminiModel("gemini-embedding-001", "Gemini Embedding 001", "Specialized", f"{DOCS}/model-versions",
                workflow_supported=False, note="Configured separately for memory/search embeddings."),
)
BY_ID = {model.id: model for model in MODELS}
DEFAULT_ALLOWED_MODELS = ",".join(m.id for m in MODELS if m.workflow_supported and m.family != "Retired")


def workflow_eligible(model_id: str, today: date | None = None) -> bool:
    model = BY_ID.get(model_id)
    return bool(model and model.workflow_supported and (
        not model.retirement_date or (today or date.today()).isoformat() < model.retirement_date))


def catalog_payload(configured: list[str], *, provider: str, default_model: str) -> dict:
    """Return honest availability: enabled != access verified. Preserve other providers."""
    gemini = provider in {"vertex", "gemini", "auto"}
    rows = []
    for model in MODELS if gemini else ():
        row = asdict(model)
        supported = workflow_eligible(model.id)
        enabled = supported and model.id in configured
        reason = (model.note or "Not supported for text/tool workflows.") if not model.workflow_supported else (
            "Retired on " + model.retirement_date if not supported else (
                "Not enabled by the server administrator." if not enabled else ""))
        row.update(enabled=enabled, selectable=enabled, reason=reason,
                   access="unverified", documentation_checked=CHECKED)
        rows.append(row)
    known = {row["id"] for row in rows}
    for model_id in configured:
        if model_id not in known:
            rows.append(dict(id=model_id, name=model_id, family="Configured", lifecycle="unknown",
                             workflow_supported=not gemini, enabled=not gemini, selectable=not gemini,
                             access="unverified", documentation="", documentation_checked="",
                             retirement_date="", note="", reason=(
                                 "Model ID has not been reviewed for workflow compatibility." if gemini else "")))
    return {"models": rows, "provider": provider, "default_model": default_model,
            "documentation_checked": CHECKED, "access": "unverified",
            "auth_mode": "service_account_adc" if provider == "vertex" else "provider_credentials"}
