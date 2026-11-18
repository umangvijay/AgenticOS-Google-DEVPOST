"""Deliverable verification (Phase 13 §13.29).

A run cannot be verified solely because a download link exists. The
deliverable resolves through an AUTHORIZED registry with:
- ownership (only the owner — or an explicit grant — may verify),
- content checksum (content-addressed identity),
- format check (the declared content_type is honored),
- provenance integration: resume/doc fields verify against supplied
  sources and unsupported facts are flagged (§17.3 machinery).

This is the registry the engine's artifact_exists criterion resolver
points at; verification results feed the evaluation record's evidence.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Optional


class DeliverableRegistry:
    """The authorized artifact registry (SQLite-backed in production; the
    in-memory reference here carries the contract)."""

    def __init__(self):
        self._artifacts: Dict[str, Dict[str, Any]] = {}

    async def register(self, artifact_id: str, *, owner_user_id: str,
                       content: Any, content_type: str) -> Dict[str, Any]:
        checksum = hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode()
        ).hexdigest()
        record = {
            "artifact_id": artifact_id, "owner_user_id": owner_user_id,
            "content": content, "content_type": content_type,
            "checksum": checksum, "registered_at": 0,
        }
        self._artifacts[artifact_id] = record
        return record

    def get(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        return self._artifacts.get(artifact_id)


async def verify_deliverable(artifact_id: str, *, owner_user_id: str,
                             registry: DeliverableRegistry,
                             provenance_report: Optional[Dict[str, Any]] = None,
                             expected_format: Optional[str] = None,
                             ) -> Dict[str, Any]:
    """§13.29: existence + ownership + integrity + format + factuality.
    Inaccessible, malformed, or materially unsupported artifacts fail or
    require review — never pass silently."""
    record = registry.get(artifact_id)
    if record is None or record.get("owner_user_id") != owner_user_id:
        # Cross-tenant reads look identical to missing (§12.28).
        return {"verified": False, "reason": "not_found",
                "artifact_id": artifact_id}

    checksum = hashlib.sha256(
        json.dumps(record.get("content"), sort_keys=True, default=str).encode()
    ).hexdigest()
    if checksum != record.get("checksum"):
        return {"verified": False, "reason": "checksum_mismatch",
                "artifact_id": artifact_id}

    if expected_format is not None and \
            record.get("content_type") != expected_format:
        return {"verified": False, "reason": "format_mismatch",
                "artifact_id": artifact_id,
                "expected": expected_format,
                "actual": record.get("content_type")}

    # §13.29 factuality: unsupported facts force review, never pass.
    factuality: Dict[str, Any] = {"status": "not_applicable"}
    if provenance_report is not None:
        unsupported = provenance_report.get("unsupported") or []
        factuality = {
            "status": "review" if unsupported else "verified",
            "unsupported_facts": len(unsupported),
        }

    verified = factuality["status"] != "review"
    return {
        "verified": verified,
        "reason": None if verified else "unsupported_facts_require_review",
        "artifact_id": artifact_id,
        "checksum": checksum,
        "content_type": record.get("content_type"),
        "factuality": factuality,
    }
