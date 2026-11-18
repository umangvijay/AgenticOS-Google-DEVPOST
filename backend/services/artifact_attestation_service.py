"""Trusted validation attestations (Phase 7 §7.19).

Deployment authority NEVER depends on caller-supplied booleans. An
attestation is published ONLY by the trusted validation service, is bound to
the exact artifact + runtime + suite digests it observed, and is verified by
an HMAC the generated process can never access (the signing authority lives
in the control plane, outside any sandbox).

Acceptance tests (§7.19): a forged result document is rejected; evidence for
one artifact cannot authorize another; an untrusted issuer cannot publish
authoritative evidence; retesting appends history instead of rewriting.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

# The trusted issuer's signing key lives ONLY in the control plane (settings/
# secret store) — never passed to runners or generated code. Auto-generated
# per process for development; deployments inject it.
_ATTESTATION_KEY: Optional[bytes] = None


def _key() -> bytes:
    global _ATTESTATION_KEY
    if _ATTESTATION_KEY is None:
        import os
        from backend.config.settings import settings
        configured = os.environ.get("ATTESTATION_SIGNING_KEY")
        if settings.APP_ENV.lower() == "production" and (
                not configured or len(configured.encode()) < 32):
            raise ValueError("Production attestations require a shared ATTESTATION_SIGNING_KEY of at least 32 bytes")
        _ATTESTATION_KEY = configured.encode() if configured else secrets.token_bytes(32)
    return _ATTESTATION_KEY


def set_attestation_key(key: bytes) -> None:
    global _ATTESTATION_KEY
    _ATTESTATION_KEY = key


REQUIRED_ATTESTATION_FIELDS = (
    "artifact_digest", "runtime_image_digest", "runner_profile_digest",
    "test_suite_digest", "test_run_id", "observed_results",
)


def issue_attestation(*, artifact_digest: str, runtime_image_digest: str,
                      runner_profile_digest: str, test_suite_digest: str,
                      test_run_id: str, issuer_identity: str,
                      observed_results: Dict[str, Any],
                      evidence_references: Optional[List[str]] = None) -> Dict[str, Any]:
    """§7.19: the TRUSTED validation service publishes an attestation derived
    from OBSERVED results (protocol messages, process state, fixture
    observations) — never from a workload's printed claims. A generated
    server printing "all tests passed" is just output."""
    for f in REQUIRED_ATTESTATION_FIELDS:
        if not locals().get(f):
            raise ValueError(f"Attestation requires {f}")
    attestation = {
        "attestation_id": f"att_{uuid.uuid4().hex[:20]}",
        "artifact_digest": artifact_digest,
        "runtime_image_digest": runtime_image_digest,
        "runner_profile_digest": runner_profile_digest,
        "test_suite_digest": test_suite_digest,
        "test_run_id": test_run_id,
        "issuer_identity": issuer_identity,
        "started_at": epoch_now(),
        "completed_at": epoch_now(),
        "observed_results": observed_results,
        "evidence_references": evidence_references or [],
        "schema_version": "p7.v1",
    }
    payload = json.dumps(attestation, sort_keys=True).encode()
    attestation["signature"] = hmac.new(_key(), payload,
                                        hashlib.sha256).hexdigest()
    return attestation


def verify_attestation(attestation: Dict[str, Any]) -> bool:
    """Verify issuer authenticity and field integrity. A forged document
    (supplied passing flags, edited results) fails the HMAC check; evidence
    for one artifact cannot authorize another because the artifact digest is
    inside the signed payload."""
    if not isinstance(attestation, dict) or not isinstance(attestation.get("signature"), str):
        return False
    body = {k: v for k, v in attestation.items() if k != "signature"}
    try:
        encoded = json.dumps(body, sort_keys=True).encode()
    except (TypeError, ValueError):
        return False
    expected = hmac.new(
        _key(), encoded, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, attestation["signature"])


def attestation_covers(attestation: Dict[str, Any], *, artifact_digest: str,
                       runtime_image_digest: str,
                       test_suite_digest: str) -> bool:
    """§7.19/§7.20: the attestation authorizes EXACTLY this artifact, this
    runtime, this suite — nothing else."""
    return (verify_attestation(attestation)
            and attestation.get("artifact_digest") == artifact_digest
            and attestation.get("runtime_image_digest") == runtime_image_digest
            and attestation.get("test_suite_digest") == test_suite_digest)


def content_digest_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
