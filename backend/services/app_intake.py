"""Application-intake record (§29.14).

Trustworthy intake happens BEFORE generation: the build persists WHAT
was supplied (source digest, declared kind, requested operations),
what was RETRIEVED (retrieval time, source URI, spec digest), what the
spec actually documents (interfaces + auth requirements) and what
remains ASSUMED — so the resulting proposal is bounded by evidence
instead of invention. AgentOS does not invent an API from a website
URL: an unsupported access mode is recorded as the missing prerequisite.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Declared intake kind → the execution mode the proposal can honestly
# support. "browser" means an authorized browser workflow; "unsupported"
# records that no supported interface exists yet.
EXECUTION_MODES = {
    "spec": "http_adapter",
    "openapi": "http_adapter",
    "url": "http_adapter",
    "prompt": "http_adapter_or_unsupported",
    "python_tools": "standalone_server",
    "website": "browser",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_digest(source: str) -> str:
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


def intake_at_admission(*, method: str, source: str,
                        name: Optional[str],
                        required_operations: Optional[List[str]] = None,
                        credential_reference: Optional[str] = None,
                        origin: str = "") -> Dict[str, Any]:
    """The record created BEFORE any generation runs."""
    return {
        "record_version": 1,
        "stage": "admitted",
        "declared_source_kind": method,
        "supported_execution_mode": EXECUTION_MODES.get(method, "unsupported"),
        "source_digest": source_digest(source),
        "source_length": len(source or ""),
        "display_name": name,
        "requested_operations": list(required_operations or []),
        "credential_reference_declared": bool(credential_reference),
        "origin": origin,
        "admitted_at": utc_now_iso(),
        # Unknown until retrieval completes — explicit, never guessed.
        "retrieved_at": None,
        "retrieval_source_uri": None,
        "documented_operations": [],
        "authentication_requirements": [],
        "unresolved_assumptions": [],
    }


def finalize_intake_record(intake: Dict[str, Any], *, source_uri: Optional[str],
                           spec_digest: str,
                           documented_operations: List[Dict[str, Any]],
                           authentication_requirements: List[str],
                           unresolved_assumptions: Optional[List[str]] = None,
                           ) -> Dict[str, Any]:
    """The record after retrieval + parsing — documented interfaces and
    auth requirements come from the RETRIEVED spec only."""
    finalized = dict(intake)
    finalized.update({
        "stage": "retrieved",
        "retrieved_at": utc_now_iso(),
        "retrieval_source_uri": source_uri,
        "retrieved_digest": spec_digest,
        "digest_matches_admission": None,  # set below when comparable
        "documented_operations": documented_operations,
        "authentication_requirements": authentication_requirements,
        "unresolved_assumptions": list(unresolved_assumptions or []),
    })
    if finalized.get("source_digest") and spec_digest:
        finalized["digest_matches_admission"] = \
            finalized["source_digest"] == spec_digest
    return finalized


def documented_operations_from_model(model: Any) -> List[Dict[str, Any]]:
    """The operations the SPEC documents (not inferred capabilities)."""
    out = []
    for op in getattr(model, "operations", None) or []:
        out.append({
            "operation_id": getattr(op, "operation_id", None),
            "method": getattr(op, "http_method", None),
            "path": getattr(op, "path", None),
            "summary": (getattr(op, "summary", "") or "")[:160],
        })
    return out


def authentication_requirements_from_auth(auth: Dict[str, Any]) -> List[str]:
    """What the spec says it needs (scheme names only — never secrets)."""
    if not auth:
        return []
    scheme = auth.get("type") or auth.get("scheme") or "none"
    requirements = [str(scheme).lower()]
    scopes = auth.get("scopes") or auth.get("granted_scopes") or []
    if scopes:
        requirements.append("scopes:" + ",".join(sorted(str(s) for s in scopes)))
    return requirements


def serialize_intake(intake: Dict[str, Any]) -> str:
    return json.dumps(intake, sort_keys=True, default=str)


def deserialize_intake(raw: Optional[str]) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def forge_record(*, kind: str, method: str, source_uri: Optional[str],
                 raw_digest: str, effective_spec_digest: str,
                 normalizer_version: str,
                 operations: List[Dict[str, Any]],
                 spec_provenance: str = "",
                 base_revision: Optional[str] = None,
                 ) -> Dict[str, Any]:
    """§29.14/§29.15 revision: the persisted Forge record — the product
    identity of one build: WHAT was retrieved (resolved URI, raw vs
    normalized digests), WHAT was built (kind, per-operation digests,
    effect classes), and what it derives from (base revision). Secret
    references never enter this record (§6.3)."""
    return {
        "record_version": 1,
        "kind": kind,
        "method": method,
        "source_uri": source_uri,
        "raw_digest": raw_digest,
        "effective_spec_digest": effective_spec_digest,
        "normalizer_version": normalizer_version,
        "spec_provenance": spec_provenance,
        "base_revision": base_revision,
        "operations": operations,
    }


def operations_forged(tools: List[Dict[str, Any]],
                      spec_hash: str) -> List[Dict[str, Any]]:
    """The selected operations with their immutable definition digests
    (`definition_digest` — the SAME digest validation evidence binds to)
    plus the effect class inferred from the HTTP method."""
    from backend.services.tool_validation import definition_digest
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            tool = tool.model_dump(mode="json")
        operation = tool.get("operation") or {}
        method = str(operation.get("http_method") or "GET").upper()
        out.append({
            "id": tool.get("tool_name"),
            "definition_digest": definition_digest(tool, spec_hash),
            "effect_class": "read" if method in ("GET", "HEAD", "OPTIONS")
            else "write",
            "pagination": "opaque_cursor"
            if "cursor" in json.dumps(operation.get("parameters") or [])
            .lower() else "none",
        })
    return out


NORMALIZER_VERSION = "openapi-normalizer-v2"
