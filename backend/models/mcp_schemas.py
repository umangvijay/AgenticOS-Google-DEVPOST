from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from enum import Enum
from backend.models.security import RiskLevel

class AuthType(str, Enum):
    NONE = "NONE"
    OAUTH2 = "OAUTH2"
    API_KEY = "API_KEY"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"

class AuthMetadata(BaseModel):
    type: AuthType = AuthType.NONE
    credential_ref: Optional[str] = Field(None, description="Reference to Secret Manager or Config")
    # §17.5: the binding CONTEXT is explicit and survives serialization —
    # validation under credential version A cannot authorize execution
    # under version B. When a credential_ref is present the version and
    # epoch are REQUIRED (no silent "v0"/"e0" defaults).
    secret_version: Optional[str] = Field(
        None, description="Credential secret version binding (§17.5)")
    authorization_epoch: Optional[str] = Field(
        None, description="Authorization epoch binding (§17.5)")

    legacy_unbound: bool = False

    @model_validator(mode="after")
    def _require_binding_context(self):
        # §17.5: a credential binding WITHOUT version/epoch context is a
        # LEGACY record — tolerated at parse (persisted rows predate the
        # field) but marked explicitly, and its binding id cannot match
        # any fresh validation evidence (the placeholder hash differs).
        if self.credential_ref and (
                not self.secret_version or not self.authorization_epoch):
            object.__setattr__(self, "legacy_unbound", True)
            self.secret_version = self.secret_version or "legacy"
            self.authorization_epoch = self.authorization_epoch or "legacy"
        return self

    def binding_context(self) -> tuple:
        """The (ref, version, epoch) triple the validation binding
        hashes — matching credential_binding_id_from exactly."""
        if not self.credential_ref:
            return ("no-auth", self.authorization_epoch or "e0",
                    self.secret_version or "v0")
        return (self.credential_ref, self.secret_version or "v0",
                self.authorization_epoch or "e0")

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_auth_type(cls, value):
        if value is None or value == "":
            return AuthType.NONE
        raw = str(value).upper().replace(" ", "_")
        aliases = {
            "OAUTH": AuthType.OAUTH2,
            "OAUTH2": AuthType.OAUTH2,
            "BASIC": AuthType.API_KEY,
            "BEARER": AuthType.API_KEY,
            "APIKEY": AuthType.API_KEY,
            "API_KEY": AuthType.API_KEY,
            "NONE": AuthType.NONE,
            "SERVICE_ACCOUNT": AuthType.SERVICE_ACCOUNT,
        }
        return aliases.get(raw, value)

class ConnectorState(str, Enum):
    DRAFT = "DRAFT"
    PENDING_CREDENTIALS = "PENDING_CREDENTIALS"
    VALIDATING = "VALIDATING"
    HEALTHY = "HEALTHY"
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    FAILED = "FAILED"

class MCPHealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"

class MCPTransportType(str, Enum):
    STREAMABLE_HTTP = "streamable_http"
    STDIO = "stdio"
    INTERNAL = "internal"

class MCPManifest(BaseModel):
    mcp_id: str
    name: str
    version: str
    endpoint: str
    transport: MCPTransportType
    auth: AuthMetadata = Field(default_factory=AuthMetadata)
    scopes: List[str] = Field(default_factory=list)
    health: MCPHealthStatus = MCPHealthStatus.UNKNOWN
    health_updated_at: Optional[datetime] = None
    state: ConnectorState = ConnectorState.DRAFT
    spec_hash: Optional[str] = None
    spec_version: Optional[str] = None
    source_uri: Optional[str] = None
    built_at: Optional[datetime] = None
    description: str = ""
    active_revision_id: Optional[str] = None
    trust_tier: str = "pending_review"
    source_type: str = "openapi"
    # Audit G06: where the operation definitions came from. official_spec |
    # extracted_docs | inferred_description. The provenance is the trust
    # ceiling: inferred sketches never reach the verified tier.
    spec_provenance: str = "official_spec"
    # Audit G03: explicit artifact products. "adapter" (default, the active
    # product) is a declarative mapping run by the shared runtime;
    # "standalone_server" is an explicit export package that is never
    # deployable until its protocol tests pass.
    artifact_type: str = "adapter"
    # Audit G02: per-operation test coverage summary. A passing probe is
    # evidence for exactly one operation.
    test_coverage: Dict[str, Any] = {}
    capabilities_omitted: int = 0
    spec_json: Optional[str] = None
    owner: str = "system"
    is_enabled: bool = True
    is_public: bool = False   # listed in the shared catalog (verified tier only)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class OperationAuthRequirement(BaseModel):
    auth_scheme: str
    required_scopes: List[str] = Field(default_factory=list)

class CachedToolDefinition(BaseModel):
    tool_name: str
    description: str
    input_schema: Dict[str, Any]
    mcp_id: str
    mcp_version: str
    discovered_at: datetime
    expires_at: datetime
    # Phase 4: Operation-level auth binding
    auth_requirements: List[OperationAuthRequirement] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.CRITICAL
    operation: Optional[Dict[str, Any]] = None

class ToolPolicyResult(BaseModel):
    allowed: bool
    reason: Optional[str] = None
