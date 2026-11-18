"""
AgentOS — Key envelope (Phase 30 / 30.7)

Tenant/purpose-bound envelope encryption for locally encrypted payloads,
plus version-qualified managed-secret references.

Envelope format ``env1`` (compact msgpack-style JSON, base64url, after the
``env1:`` tag):

    {
      "fmt": "env1",
      "alg": "AES-256-GCM",
      "kid": "local:v1"            # wrapping-key reference (version-qualified)
                                   #   or "gcp://<secret>@<version>"
      "wiv": "<b64 wrapping nonce>",
      "wrap": "<b64 wrapped data-encryption key>",
      "iv": "<b64 record nonce>",
      "ct": "<b64 authenticated ciphertext>",
    }

Each payload gets a FRESH data-encryption key (DEK). The DEK is wrapped
with a versioned key-encryption key (KEK) derived from the master secret
via HKDF with a version-specific info label (or a managed-secret KEK).
The record ciphertext authenticates the CANONICAL ASSOCIATED DATA:

    aad = "veyrune-envelope:env1|<tenant>|<credential_id>|<purpose>"

so a record sealed for one tenant/purpose can never be opened under
another — AES-GCM authentication fails before any plaintext exists.
Rewrapping (key rotation) re-wraps only the DEK; the record ciphertext
and its AAD binding are untouched.

Managed-secret storage persists a VERSION-QUALIFIED reference
(``gcp://<secret>@<version>``) so rotation upstream cannot silently
change what a stored reference resolves to.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from backend.config.settings import settings

logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "env1:"
ENVELOPE_FORMAT = "env1"
ALGORITHM = "AES-256-GCM"
NONCE_SIZE = 12
KEY_SIZE = 32
AAD_PREFIX = "veyrune-envelope"
WRAP_INFO_TEMPLATE = "veyrune:envelope:kek:{version}"
# Maximum accepted AAD component length — bounds the canonical form.
MAX_AAD_COMPONENT = 256


class EnvelopeError(ValueError):
    """Raised when an envelope cannot be opened under the given binding."""


def canonical_aad(tenant_id: str, credential_id: str, purpose: str,
                  fmt: str = ENVELOPE_FORMAT) -> bytes:
    """Canonical associated data binding tenant, credential and purpose.

    The envelope format itself is authenticated so a future format can
    never be downgraded into an older one silently."""
    for name, component in (("tenant", tenant_id), ("credential", credential_id),
                            ("purpose", purpose)):
        if not component or not isinstance(component, str):
            raise EnvelopeError(f"envelope binding requires a non-empty {name} identifier")
        if len(component) > MAX_AAD_COMPONENT:
            raise EnvelopeError(f"{name} identifier too long for canonical AAD")
    return "|".join([
        f"{AAD_PREFIX}:{fmt}", tenant_id, credential_id, purpose,
    ]).encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def wrapping_key_ref() -> str:
    """Version-qualified reference of the active local wrapping key."""
    version = getattr(settings, "ENVELOPE_KEK_VERSION", None) or "v1"
    return f"local:{version}"


# One ephemeral master per process when SECRETS_MASTER_KEY is unset —
# sealing and opening within the process stay consistent (matches the
# vault's dev behaviour: nothing persists across restarts).
_EPHEMERAL_MASTER: Optional[str] = None


def _local_kek(version: str) -> bytes:
    """Derive the versioned key-encryption key from the master secret."""
    global _EPHEMERAL_MASTER
    master = settings.SECRETS_MASTER_KEY
    if not master:
        if not _EPHEMERAL_MASTER:
            _EPHEMERAL_MASTER = _b64e(os.urandom(32))
            logger.warning(
                "SECRETS_MASTER_KEY is not set; envelope wrapping keys are ephemeral."
            )
        master = _EPHEMERAL_MASTER
    info = WRAP_INFO_TEMPLATE.format(version=version).encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_SIZE, salt=None, info=info,
    ).derive(master.encode("utf-8"))


@dataclass
class EnvelopeBinding:
    """Identity a sealed record is bound to (authenticated via AAD)."""
    tenant_id: str
    credential_id: str
    purpose: str


@dataclass
class Envelope:
    """A sealed record: authenticated ciphertext + wrapped DEK + references."""
    binding: EnvelopeBinding
    kid: str
    wrapping_nonce: bytes
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes
    fmt: str = ENVELOPE_FORMAT
    algorithm: str = ALGORITHM
    metadata: Dict[str, str] = field(default_factory=dict)

    def pack(self) -> str:
        payload = {
            "fmt": self.fmt,
            "alg": self.algorithm,
            "kid": self.kid,
            "wiv": _b64e(self.wrapping_nonce),
            "wrap": _b64e(self.wrapped_dek),
            "iv": _b64e(self.nonce),
            "ct": _b64e(self.ciphertext),
        }
        meta = dict(self.metadata)
        meta.update({"tenant": self.binding.tenant_id,
                     "credential": self.binding.credential_id,
                     "purpose": self.binding.purpose})
        payload["meta"] = meta
        return ENVELOPE_PREFIX + _b64e(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )

    @classmethod
    def unpack(cls, packed: str) -> "Envelope":
        if not packed.startswith(ENVELOPE_PREFIX):
            raise EnvelopeError("not an env1 envelope")
        try:
            payload = json.loads(_b64d(packed[len(ENVELOPE_PREFIX):]).decode("utf-8"))
        except Exception as exc:
            raise EnvelopeError("malformed envelope encoding") from exc
        try:
            binding = EnvelopeBinding(
                tenant_id=payload["meta"]["tenant"],
                credential_id=payload["meta"]["credential"],
                purpose=payload["meta"]["purpose"],
            )
            metadata = {k: str(v) for k, v in payload.get("meta", {}).items()
                        if k not in ("tenant", "credential", "purpose")}
            return cls(
                binding=binding,
                kid=str(payload["kid"]),
                wrapping_nonce=_b64d(payload["wiv"]),
                wrapped_dek=_b64d(payload["wrap"]),
                nonce=_b64d(payload["iv"]),
                ciphertext=_b64d(payload["ct"]),
                fmt=str(payload.get("fmt", ENVELOPE_FORMAT)),
                algorithm=str(payload.get("alg", ALGORITHM)),
                metadata=metadata,
            )
        except (KeyError, TypeError) as exc:
            raise EnvelopeError(f"envelope missing required field: {exc}") from exc


def seal_envelope(plaintext: str, *, tenant_id: str, credential_id: str,
                  purpose: str, metadata: Optional[Dict[str, str]] = None) -> str:
    """Seal ``plaintext`` into an env1 envelope bound to the given identity."""
    binding = EnvelopeBinding(tenant_id=tenant_id, credential_id=credential_id,
                              purpose=purpose)
    aad = canonical_aad(binding.tenant_id, binding.credential_id, binding.purpose)

    dek = os.urandom(KEY_SIZE)
    record_nonce = os.urandom(NONCE_SIZE)
    ciphertext = AESGCM(dek).encrypt(record_nonce, plaintext.encode("utf-8"), aad)

    kid = wrapping_key_ref()
    kek = _resolve_kek(kid)
    wrap_nonce = os.urandom(NONCE_SIZE)
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, aad)

    meta = dict(metadata or {})
    meta.update({"tenant": binding.tenant_id, "credential": binding.credential_id,
                 "purpose": binding.purpose})
    envelope = Envelope(binding=binding, kid=kid, wrapping_nonce=wrap_nonce,
                        wrapped_dek=wrapped, nonce=record_nonce,
                        ciphertext=ciphertext, metadata=meta)
    return envelope.pack()


def _resolve_kek(kid: str) -> bytes:
    if kid.startswith("local:"):
        return _local_kek(kid.split(":", 1)[1])
    if kid.startswith("gcp://"):
        # Managed wrapping key: the DEK is wrapped under the CURRENT value
        # of the referenced (version-qualified) managed secret.
        ref = kid.split("gcp://", 1)[1]
        from backend.security.gcp_secret_manager import GCPSecretManager
        client = GCPSecretManager()
        if "@" in ref:
            secret_id, version = ref.split("@", 1)
            return client.get_secret_version(secret_id, version).encode("utf-8")
        return client.get_secret(ref).encode("utf-8")
    raise EnvelopeError(f"unknown wrapping key reference {kid!r}")


def open_envelope(packed: str, *, tenant_id: str, credential_id: str,
                  purpose: str) -> str:
    """Open an env1 envelope under the given binding.

    Raises EnvelopeError when the record was sealed for a different
    tenant/credential/purpose, when the wrapping key does not match, or
    when the ciphertext fails authentication (wrong/tampered)."""
    envelope = Envelope.unpack(packed)
    aad = canonical_aad(tenant_id, credential_id, purpose, envelope.fmt)
    # The stored binding must agree with the caller-supplied identity —
    # opening with an unrelated identity is refused before any key work.
    if (envelope.binding.tenant_id != tenant_id
            or envelope.binding.credential_id != credential_id
            or envelope.binding.purpose != purpose):
        raise EnvelopeError(
            "envelope binding mismatch: record is sealed for a different "
            "tenant/credential/purpose"
        )
    try:
        kek = _resolve_kek(envelope.kid)
        dek = AESGCM(kek).decrypt(envelope.wrapping_nonce, envelope.wrapped_dek, aad)
        plaintext = AESGCM(dek).decrypt(envelope.nonce, envelope.ciphertext, aad)
        return plaintext.decode("utf-8")
    except EnvelopeError:
        raise
    except Exception as exc:
        raise EnvelopeError(
            "envelope authentication failed — wrong tenant/purpose/key or tampered record"
        ) from exc


def rewrap_envelope(packed: str, *, to_kid: Optional[str] = None) -> str:
    """Re-wrap the DEK under a different key-encryption key.

    Wrapping-key rotation does NOT touch the record ciphertext, the AAD
    binding, or the upstream credential — the record simply becomes
    readable under the new wrapping key version."""
    envelope = Envelope.unpack(packed)
    aad = canonical_aad(envelope.binding.tenant_id, envelope.binding.credential_id,
                        envelope.binding.purpose, envelope.fmt)
    old_kek = _resolve_kek(envelope.kid)
    dek = AESGCM(old_kek).decrypt(envelope.wrapping_nonce, envelope.wrapped_dek, aad)
    new_kid = to_kid or wrapping_key_ref()
    if new_kid == envelope.kid:
        return packed  # already wrapped under the requested key
    new_kek = _resolve_kek(new_kid)
    wrap_nonce = os.urandom(NONCE_SIZE)
    wrapped = AESGCM(new_kek).encrypt(wrap_nonce, dek, aad)
    envelope.kid = new_kid
    envelope.wrapping_nonce = wrap_nonce
    envelope.wrapped_dek = wrapped
    return envelope.pack()


def is_envelope(value: Optional[str]) -> bool:
    return bool(value) and value.startswith(ENVELOPE_PREFIX)


def parse_managed_reference(value: str) -> Tuple[str, Optional[str]]:
    """Split a ``gcp://secret[@version]`` reference into (secret_id, version).

    Version-qualified references pin rotation: the stored record keeps
    resolving to exactly the version it was written against."""
    ref = value.split("gcp://", 1)[1] if value.startswith("gcp://") else value
    if "@" in ref:
        secret_id, version = ref.split("@", 1)
        return secret_id, (version or None)
    return ref, None


def decrypt_credential_payload(encrypted: str, *, tenant_id: str,
                               credential_id: str, purpose: str,
                               vault=None) -> str:
    """DUAL READER (30.7 step 2): read every stored generation.

    Dispatch order:
      - ``env1:``      → tenant/purpose-bound envelope (current format)
      - ``gcp://id@v`` → version-qualified managed secret (pinned)
      - ``gcp://id``   → managed secret, latest version (legacy reference)
      - otherwise      → SecretsVault v2 / legacy PBKDF2 ciphertext
    """
    vault = vault
    if is_envelope(encrypted):
        return open_envelope(encrypted, tenant_id=tenant_id,
                             credential_id=credential_id, purpose=purpose)
    if encrypted.startswith("gcp://"):
        secret_id, version = parse_managed_reference(encrypted)
        if vault is not None and vault.use_gcp:
            if version:
                return vault.gcp_client.get_secret_version(secret_id, version)
            return vault.gcp_client.get_secret(secret_id)
        from backend.security.gcp_secret_manager import GCPSecretManager
        client = GCPSecretManager()
        if version:
            return client.get_secret_version(secret_id, version)
        return client.get_secret(secret_id)
    # Legacy generations go through the vault (v2 HKDF/AES-GCM and the
    # read-only PBKDF2 path). Helpers that run outside app startup (the
    # restore drill, CLI tools) may hold an uninitialized singleton —
    # initialize it lazily rather than failing the read.
    if vault is None:
        from backend.security.secrets_vault import secrets_vault as vault
    if not getattr(vault, "_initialized", False):
        vault.initialize()
    return vault.decrypt(encrypted)


def seal_for_credential(plaintext: str, *, owner_id: str, name: str,
                        purpose: str = "credential-store") -> str:
    """Seal a credential payload for one owner (the credential's tenant).

    The tenant for user-scoped credentials is the owner's identity; org
    credentials pass the organization id as ``owner_id`` at their call
    sites so the binding matches whoever may read them."""
    return seal_envelope(
        plaintext,
        tenant_id=owner_id,
        credential_id=f"cred:{name}",
        purpose=purpose,
        metadata={"format": "credential-values-json"},
    )
