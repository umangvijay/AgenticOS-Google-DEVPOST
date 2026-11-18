"""Credential binding (Phase 6 §6.7).

Binds validation evidence and execution eligibility to a specific
authorization context — NOT to a raw secret. The binding changes when the
account identity, granted scopes, or authorization-relevant properties
change; routine OAuth token refresh does NOT change the epoch (token bytes
are not authorization identity), but account switches and scope reductions
do. Raw secrets never enter any of these records (§6.3).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BindingStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


# §6.7: supported schemes, represented explicitly. Unsupported schemes are
# REJECTED, not coerced into a superficially similar type.
AUTH_SCHEMES = (
    "none", "api_key_header", "api_key_query", "http_basic",
    "http_bearer", "oauth2", "service_account",
)


@dataclass
class CredentialBinding:
    """§6.7 — which account and authorization a connector uses."""

    binding_id: str
    owner_or_tenant_id: str
    secret_reference: str            # internal reference, NEVER the secret
    secret_version: str = "v0"
    authorization_epoch: str = "e0"  # changes on account/scope/grant change
    provider_account_id: Optional[str] = None
    granted_scopes: tuple = ()
    environment: str = "production"  # production | sandbox | development
    status: BindingStatus = BindingStatus.ACTIVE

    def context_digest(self) -> str:
        """The validation-context identity: evidence binds to THIS triple.
        A secret rotation bumps secret_version (tests may carry over per
        policy); an account or scope change bumps the epoch (account-bound
        evidence is stale)."""
        return hashlib.sha256(
            f"{self.secret_reference}|{self.secret_version}|"
            f"{self.authorization_epoch}|{self.provider_account_id or ''}|"
            f"{self.environment}".encode()).hexdigest()[:32]

    def is_current_for(self, context_digest: str) -> bool:
        return (self.status is BindingStatus.ACTIVE
                and self.context_digest() == context_digest)


def binding_from_secret_ref(secret_reference: str, *,
                            secret_version: str = "v0",
                            authorization_epoch: str = "e0",
                            provider_account_id: Optional[str] = None,
                            environment: str = "production") -> CredentialBinding:
    binding_id = hashlib.sha256(
        f"{secret_reference}|{secret_version}|{authorization_epoch}|"
        f"{provider_account_id or ''}|{environment}".encode()).hexdigest()[:32]
    return CredentialBinding(
        binding_id=binding_id, owner_or_tenant_id="",
        secret_reference=secret_reference, secret_version=secret_version,
        authorization_epoch=authorization_epoch,
        provider_account_id=provider_account_id, environment=environment)
