"""Deliverable contract (Phase 17 §17.6) — the durable record of WHAT
was produced, by which task, from which sources, with which verification
evidence. The existing artifact builder remains the producer; this is
the delivery record, NOT a second artifact system (and NOT the
MCP-specific artifact contract, whose meaning differs)."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SUPPORTED_TYPES = ("resume_html", "resume_pdf", "factual_report",
                   "source_project", "tested_project")
LIFECYCLE_STATES = ("generated", "stored", "verification_completed",
                    "verification_passed", "verification_failed",
                    "review_completed", "delivered", "deleted")

# §17.1: the deliberately narrow supported set and its verification
# requirements — declared per format, with what the product does NOT
# yet verify stated explicitly.
SUPPORTED_DELIVERABLES: Dict[str, Dict[str, Any]] = {
    "resume_html": {
        "required_inputs": ["profile facts", "job description (optional)"],
        "required_sections": ["basics", "experience"],
        "viewers": ["browser"],
        "verification": ["structured data valid", "factual claims reviewed",
                         "safe rendering", "final bytes registered"],
        "max_bytes": 2_000_000,
        "not_yet_verified": ["PDF-specific layout", "third-party ATS parsing"],
    },
    "resume_pdf": {
        "required_inputs": ["rendered HTML revision"],
        "required_sections": ["basics", "experience"],
        "viewers": ["PDF viewer"],
        "verification": ["all HTML requirements", "PDF parses",
                         "text extractable", "final bytes registered"],
        "max_bytes": 10_000_000,
        "not_yet_verified": ["visual layout review (needs a viewer fixture)"],
    },
    "factual_report": {
        "required_inputs": ["authorized sources with provenance"],
        "required_sections": ["summary", "findings"],
        "viewers": ["browser", "markdown viewer"],
        "verification": ["claim-to-source mapping", "calculations checked",
                         "limitations explicit", "final bytes registered"],
        "max_bytes": 5_000_000,
        "not_yet_verified": ["source freshness beyond retrieval time"],
    },
    "source_project": {
        "required_inputs": ["approved manifest"],
        "required_sections": ["manifest", "entrypoint"],
        "viewers": ["code viewer"],
        "verification": ["complete manifest", "content integrity",
                         "HONEST execution status (unexecuted)"],
        "max_bytes": 20_000_000,
        "not_yet_verified": ["execution (no managed runner backend yet)"],
    },
    "tested_project": {
        "required_inputs": ["source project + isolated build evidence"],
        "required_sections": ["manifest", "entrypoint"],
        "viewers": ["code viewer"],
        "verification": ["all source-project requirements",
                         "isolated build/test evidence"],
        "max_bytes": 50_000_000,
        "not_yet_verified": ["runtime smoke test (runner unavailable)"],
    },
}


def new_deliverable_id() -> str:
    return f"dlv_{uuid.uuid4().hex[:20]}"


def content_checksum(files: List[Dict[str, Any]]) -> str:
    """§17.8: hash the ACTUAL bytes (the manifest's per-file sha256 set,
    sorted by path) — never a string conversion of binary objects."""
    digest = hashlib.sha256()
    for f in sorted(files or [], key=lambda x: x.get("path", "")):
        digest.update(str(f.get("path", "")).encode())
        digest.update(b"\0")
        digest.update(str(f.get("sha256", "")).encode())
        digest.update(b"\0")
        digest.update(str(f.get("byte_length", 0)).encode())
    return digest.hexdigest()[:32]


@dataclass
class Deliverable:
    deliverable_id: str
    deliverable_key: str
    revision: int
    tenant_id: str
    owner_user_id: str
    deliverable_type: str
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    mime_type: str = ""
    file_manifest: List[Dict[str, Any]] = field(default_factory=list)
    parent_revision: Optional[int] = None
    lineage: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "generated"
    access_policy: str = "owner_only"
    content_checksum: str = ""
    created_at: int = 0
    updated_at: int = 0

    def validate(self) -> None:
        problems = []
        if self.deliverable_type not in SUPPORTED_TYPES:
            problems.append(f"unsupported deliverable type "
                            f"'{self.deliverable_type}'")
        if self.lifecycle_state not in LIFECYCLE_STATES:
            problems.append(f"unknown lifecycle state '{self.lifecycle_state}'")
        if not self.file_manifest:
            problems.append("empty file manifest")
        for f in self.file_manifest:
            if not f.get("sha256") or f.get("byte_length") in (None, 0):
                problems.append(
                    f"file '{f.get('path')}' lacks integrity metadata "
                    "(sha256 + byte length are REQUIRED, §17.8)")
        if problems:
            raise ValueError("invalid deliverable: " + "; ".join(problems))
