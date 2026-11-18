"""
AgentOS — Centralized Configuration

Dual-mode: local (SQLite) and cloud (Firestore).
Model selection is config, not a constant buried in code.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, Field
from typing import List, Optional
import json

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # ── Application ──────────────────────────────────────────────
    APP_ENV: str = "development"
    APP_NAME: str = "Sandtable"   # product name; repo/package identity remains AgentOS
    LOCAL_WORKER_CONCURRENCY: int = Field(default=4, ge=1, le=32)

    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    API_BASE_URL: str = "http://localhost:8000"

    FRONTEND_BASE_URL: str = "http://localhost:3000"
    CORS_ALLOWED_ORIGINS: List[str] = ["http://localhost:3000"]

    # ── Storage Backend ──────────────────────────────────────────
    # "sqlite" for local development, "firestore" for Google Cloud,
    # "postgres" for the §21.3 multi-process profile. This ONE setting
    # controls which repository implementations load.
    STORAGE_BACKEND: str = "sqlite"
    # §21.3: PostgreSQL connection (STORAGE_BACKEND=postgres).
    POSTGRES_DSN: str = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"
    # §21.4: durable message transport selection. "inprocess" (default,
    # single-process), "nats" (JetStream across separate processes),
    # "pubsub" (GCP).
    MESSAGE_BUS_TRANSPORT: str = "inprocess"
    # §21.5: local inference (Ollama).
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = ""
    AZURE_OPENAI_API_KEY: str = ""
    OLLAMA_MODEL: str = "qwen2.5:3b-instruct"
    # §21.5: Bedrock via Converse API.
    BEDROCK_MODEL_ID: str = "anthropic.claude-3-5-haiku-20241022-v1:0"
    AWS_REGION: str = "us-east-1"
    NATS_SERVERS: str = "nats://127.0.0.1:4222"
    # Phase 13 §13.33: progressive improvement-stage flags (comma-separated).
    IMPROVEMENT_STAGES_ENABLED: str = ""
    # §14.1: the contextual router is release-scoped — it engages only
    # when this (or 'routing' in IMPROVEMENT_STAGES_ENABLED) is set;
    # otherwise selection degrades to the baseline global ranking.
    ROUTING_STAGE_ENABLED: bool = False
    # §15.30: the specialist release stages (comma-separated) and the
    # role VERSIONS in effect — visible to operators for rollback.
    SPECIALIST_STAGES_ENABLED: str = ""
    SPECIALIST_ROLE_VERSIONS: dict = {}
    # §16.32 (finding J): the memory release stages (comma-separated) —
    # collection/retrieval/automatic-reuse independently configurable.
    MEMORY_STAGES_ENABLED: str = ""
    # §29 revision: the local-model read budget — CPU-resident inference
    # (plus queued planner/repair calls) can exceed 300 s per request.
    OLLAMA_READ_TIMEOUT_S: int = 600
    SQLITE_DB_PATH: str = "data/agentos.db"
    # §27.15/§27.13: generated file artifacts (websites, model assets,
    # documents) live HERE — explicit, because deriving the location from
    # SQLITE_DB_PATH breaks container profiles where that path points at a
    # root-owned volume (artifacts must be writable by the runtime user).
    ARTIFACTS_ROOT: str = ""

    # ── Google Cloud (Optional — only needed when STORAGE_BACKEND=firestore) ─
    GOOGLE_CLOUD_PROJECT: Optional[str] = None
    # Vertex publisher Flash models are served from location=global.
    # Override with GOOGLE_CLOUD_REGION; do not hardcode us-central1 in callers.
    GOOGLE_CLOUD_REGION: str = "global"
    FIRESTORE_DATABASE_ID: str = "(default)"

    FIRESTORE_COLLECTION_USERS: str = "users"
    FIRESTORE_COLLECTION_WORKFLOWS: str = "workflows"
    FIRESTORE_COLLECTION_RUNS: str = "runs"
    FIRESTORE_COLLECTION_TASKS: str = "tasks"

    # ── Gemini AI ────────────────────────────────────────────────
    # Flash is the default for routine work. Pro preview remains an explicit,
    # validated choice because preview model availability can change.
    GEMINI_MODEL: str = "gemini-3.8-flash"
    GEMINI_ALLOWED_MODELS: str = "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-pro-preview"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    GEMINI_EMBEDDING_REGION: str = "us-central1"
    GEMINI_API_KEY: Optional[str] = None
    # auto prefers Gemini API, then Vertex AI, then Groq, then Hugging Face. Explicit selection
    # lets operators choose Groq when multiple provider credentials exist.
    LLM_PROVIDER: str = "auto"

    # ── xAI Grok (fallback when Gemini quota/key fails) ──────────
    XAI_API_KEY: Optional[str] = None
    GROK_MODEL: str = "grok-4-fast"

    # ── Groq (OpenAI-compatible fallback) ────────────────────────
    GROQ_API_KEY: Optional[str] = None
    GROQ_MODEL: str = "openai/gpt-oss-20b"
    GROQ_REASONING_EFFORT: str = "low"

    # ── Hugging Face Inference Providers (OpenAI-compatible router) ──
    HF_TOKEN: Optional[str] = None
    HF_MODEL: str = "openai/gpt-oss-120b"

    # ── Authentication — JWT (RS256) ─────────────────────────────
    # Auto-generated on first run if missing.
    JWT_PRIVATE_KEY_PATH: str = "backend/security/keys/private.pem"
    JWT_PUBLIC_KEY_PATH: str = "backend/security/keys/public.pem"
    # Short-lived access tokens limit the impact of token theft; refresh tokens
    # are single-use and rotated through the refresh-token repository.
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "agentos"
    JWT_AUDIENCE: str = "agentos-api"
    # Optional PEM bodies (Cloud Run Secret Manager). Local uses files on disk.
    JWT_PRIVATE_KEY: Optional[str] = None
    JWT_PUBLIC_KEY: Optional[str] = None

    # ── Authentication — Google OAuth ────────────────────────────
    # Get from: https://console.cloud.google.com/apis/credentials
    # Create OAuth 2.0 Client ID (Web application). Free, no billing needed.
    GOOGLE_OAUTH_CLIENT_ID: Optional[str] = None
    GOOGLE_OAUTH_CLIENT_SECRET: Optional[str] = None

    # ── Security — Secrets Vault ─────────────────────────────────
    # Master key for AES-256-GCM encryption of stored credentials.
    # Auto-generated on first run if empty.
    SECRETS_MASTER_KEY: Optional[str] = None
    # Optional HMAC pepper mixed into password material before Argon2id.
    # Keep stable; rotation requires a password-hash migration plan.
    PASSWORD_PEPPER: Optional[str] = None

    # ── Contact form (SMTP to the founding team) ─────────────────
    CONTACT_TO_EMAIL: str = "godumang35@gmail.com"
    CONTACT_FROM_EMAIL: Optional[str] = None
    CONTACT_SMTP_HOST: Optional[str] = None
    CONTACT_SMTP_PORT: int = 587
    CONTACT_SMTP_USERNAME: Optional[str] = None
    CONTACT_SMTP_PASSWORD: Optional[str] = None
    CONTACT_WEBHOOK_URL: Optional[str] = None
    RESEND_API_KEY: Optional[str] = None

    # ── Rate Limiting ────────────────────────────────────────────
    # Workflow creation burns real Gemini quota, so it is capped tightly
    # (the audit's quota-burn loophole was a 30/min default: 10 rapid 201s
    # sailed through). Sliding-window counters are per process; on
    # multi-instance Cloud Run deployments multiply by instance count or
    # front the API with a shared limiter (APIGateway / Cloud Armor).
    RATE_LIMIT_AUTH: int = 20         # per minute on auth endpoints
    RATE_LIMIT_WORKFLOW: int = 10     # per minute on workflow creation
    RATE_LIMIT_MCP_BUILD: int = 10    # per minute on MCP builder
    RATE_LIMIT_GENERAL: int = 60      # per minute general
    # Phase 3 policy inventory (backend/security/admission_policy.py) —
    # semantics live in the registry; these are the numbers.
    RATE_LIMIT_CONTACT: int = Field(default=3, ge=0)
    RATE_LIMIT_WEBHOOK: int = Field(default=10, ge=0)
    RATE_LIMIT_RESUME: int = Field(default=10, ge=0)
    RATE_LIMIT_STUDIO: int = Field(default=20, ge=0)
    RATE_LIMIT_BROWSER_START: int = Field(default=6, ge=0)
    RATE_LIMIT_EVENT_STREAM: int = Field(default=30, ge=0)
    RATE_LIMIT_PROVIDER: int = Field(default=120, ge=0)  # per credential scope

    # ── Phase 3 capacity & backpressure ──────────────────────────
    # Per-tenant active-unit caps (capacity leases, §3.11/§3.16).
    TENANT_MAX_ACTIVE_TASKS: int = Field(default=8, ge=1)
    TENANT_MAX_ACTIVE_BUILDS: int = Field(default=2, ge=1)
    TENANT_MAX_ACTIVE_BROWSERS: int = Field(default=2, ge=1)
    # Durable pending-command caps (queue admission, §3.12).
    TENANT_MAX_PENDING_COMMANDS: int = Field(default=16, ge=1)
    # Local in-memory bus bounds (§3.13) — pending work and dead letters must
    # not grow without an application bound in local mode.
    LOCAL_QUEUE_MAXSIZE: int = Field(default=256, ge=1)
    LOCAL_DEAD_LETTER_MAXSIZE: int = Field(default=500, ge=1)
    LOCAL_MAX_PENDING_RETRIES: int = Field(default=256, ge=1)
    # How long a publisher waits for bounded-queue space before the publish
    # is REJECTED (§3.13) — natural backpressure, then a clear refusal.
    LOCAL_QUEUE_PUT_TIMEOUT: float = Field(default=10.0, ge=0.1)
    # A delayed retry older than this goes to dead letters instead of
    # looping forever through the bounded retry store.
    LOCAL_MAX_RETRY_AGE_SECONDS: int = Field(default=900, ge=1)
    # Pub/Sub subscriber flow control (§3.14) — bounds unacknowledged work
    # pulled per worker so delivery matches actual execution capacity.
    PUBSUB_MAX_OUTSTANDING_MESSAGES: int = Field(default=32, ge=1)
    PUBSUB_MAX_OUTSTANDING_BYTES: int = Field(default=52_428_800, ge=1_000_000)
    # Provider retry backoff (§3.16): bounded, jittered, Retry-After aware.
    PROVIDER_RETRY_BASE_DELAY_SECONDS: float = Field(default=0.5, ge=0)
    PROVIDER_RETRY_MAX_DELAY_SECONDS: float = Field(default=8.0, ge=0)

    # ── Token Budgets ────────────────────────────────────────────
    # Budget reservation headroom per paid call (v5.6 A3): the estimated
    # output reserve held while a model call runs, settled to actuals after.
    # Phase 1.3: organization invitations may only be accepted by accounts
    # whose email address is verified (Google claims verify it; local
    # accounts require the verification flow).
    REQUIRE_VERIFIED_EMAIL_FOR_INVITES: bool = False
    MAX_OUTPUT_RESERVE_TOKENS: int = Field(default=8192, ge=0, le=1_000_000)
    DEFAULT_DAILY_TOKEN_LIMIT: int = Field(default=1_000_000, ge=0)  # per user per day
    # Administrator-controlled ceiling: per-user daily limits clamp DOWN to
    # this. null/None means no platform-level ceiling beyond the default.
    PLATFORM_DAILY_TOKEN_LIMIT: Optional[int] = Field(default=None, ge=0)
    # Phase 2 rollout (§2.18 mixed versions): 'observe' records reservations
    # without refusing admission; 'enforce' blocks over-cap paid work. Old
    # workers that cannot reserve must be drained before flipping 'enforce'.
    BUDGET_ENFORCEMENT_MODE: str = Field(default="enforce")
    # Separate guard against unlimited outstanding exposure across days
    # (§2.14): refuses new admission when open holds exceed this share of the
    # daily limit. 1.0 disables the guard.
    MAX_OUTSTANDING_HOLD_RATIO: float = Field(default=0.5, gt=0, le=1.0)
    MAX_WORKFLOW_TASKS: int = Field(default=32, ge=1, le=256)
    MAX_TASK_TIMEOUT_SECONDS: int = Field(default=600, ge=30, le=3600)
    MAX_TASK_RETRIES: int = Field(default=3, ge=0, le=10)
    RESEARCH_MAX_HOPS: int = 10                 # max hops for research agent

    # ── Observability / cost estimates ───────────────────────────
    # USD per 1M estimated tokens, per model ("model:rate,model:rate").
    # Planning numbers only — the pipeline does not receive billing data.
    MODEL_COST_PER_1M_TOKENS: str = "gemini-3.8-flash:0.60,gemini-4-argon:2.50"

    # User-Agent for site-health probes. Some sites (e.g. Wikipedia) reject
    # generic datacenter-UA requests; a descriptive UA raises the success rate.
    HEALTH_USER_AGENT: str = "Mozilla/5.0 (compatible; SandtableHealth/2.0; +https://sandtable.dev/bot)"

    # ── Data retention (enterprise control) ──────────────────────
    # Days to keep timeline events and audit-log rows; 0 disables purging.
    # Workflow runs are never auto-deleted — only derived/observational data.
    RETENTION_DAYS: int = 90
    FEEDBACK_RETENTION_DAYS: int = Field(default=0, ge=0, description="Feedback retention window in days; 0 disables the sweep")

    @field_validator(
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "SECRETS_MASTER_KEY",
        "JWT_PRIVATE_KEY",
        "JWT_PUBLIC_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "HF_TOKEN",
        mode="before",
    )
    @classmethod
    def empty_str_none(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower().startswith("your-"):
            return None
        return s.replace("\\n", "\n") if "BEGIN" in s else s

    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def normalize_llm_provider(cls, value):
        provider = str(value or "auto").strip().lower()
        if provider not in {"auto", "gemini", "vertex", "groq", "huggingface",
                            "bedrock", "ollama", "azure"}:
            raise ValueError("LLM_PROVIDER must be auto, gemini, vertex, "
                             "groq, huggingface, bedrock, azure, or ollama")
        return provider

    @field_validator("BUDGET_ENFORCEMENT_MODE", mode="before")
    @classmethod
    def normalize_budget_enforcement_mode(cls, value):
        mode = str(value or "enforce").strip().lower()
        if mode not in {"observe", "enforce"}:
            raise ValueError("BUDGET_ENFORCEMENT_MODE must be 'observe' or 'enforce'")
        return mode

    @field_validator("GROQ_REASONING_EFFORT", mode="before")
    @classmethod
    def normalize_groq_reasoning_effort(cls, value):
        effort = str(value or "low").strip().lower()
        if effort not in {"low", "medium", "high"}:
            raise ValueError("GROQ_REASONING_EFFORT must be low, medium, or high")
        return effort

    @field_validator("CORS_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_cors(cls, v):
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            text = v.strip()
            if text.startswith("["):
                return json.loads(text)
            return [p.strip() for p in text.split(",") if p.strip()]
        return v

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
