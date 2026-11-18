"""Artifact model (Phase 7 §7.2/§7.7).

Three separate product capabilities with INDEPENDENT status fields:
- Declarative adapter (validated op-level execution, Phase 6).
- Portable source export (downloadable, marked untested until validated).
- Managed standalone execution (isolated runner; requires the most evidence).

Acceptance condition (§7.2): downloading an export cannot accidentally
activate it, and passing protocol tests cannot bypass isolation or
credential requirements. `deployable` is NOT the source of authority —
deployment eligibility is evaluated from current records by the deployment
service (§7.20).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExportStatus(str, Enum):
    NOT_EXPORTED = "not_exported"
    EXPORTED_UNTESTED = "exported_untested"
    EXPORTED = "exported"


class ProtocolValidationStatus(str, Enum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


class IsolationValidationStatus(str, Enum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NOT_SUPPORTED = "not_supported"


class DeploymentStatus(str, Enum):
    NOT_DEPLOYED = "not_deployed"
    BLOCKED = "blocked"
    ELIGIBLE = "eligible"
    DEPLOYED = "deployed"
    REVOKED = "revoked"


ARTIFACT_FORMAT_VERSION = "p7.v1"


def validate_bundle_path(relative_path: str) -> str:
    """Require one portable, unambiguous relative file path."""
    if (not isinstance(relative_path, str) or len(relative_path) > 240
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", relative_path)
            or any(part in (".", "..") for part in relative_path.split("/"))):
        raise ValueError(f"Unsafe source path: {relative_path!r}")
    return relative_path


def source_bundle_entry(relative_path: str, content: bytes,
                        file_type: str = "python") -> Dict[str, Any]:
    """§7.7: one source-bundle manifest entry — path validation, byte hash."""
    validate_bundle_path(relative_path)
    return {
        "relative_path": relative_path,
        "file_type": file_type,
        "byte_length": len(content),
        "content_digest": hashlib.sha256(content).hexdigest(),
        "relevant_file_mode": "executable" if relative_path.endswith(".py") else "data",
    }


def source_bundle_digest(entries: List[Dict[str, Any]]) -> str:
    """§7.7: hash over the deterministic, sorted bundle manifest."""
    material = sorted(entries, key=lambda e: e["relative_path"])
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def dependency_lock_digest(lock: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(lock, sort_keys=True).encode()).hexdigest()


@dataclass
class MCPArtifact:
    """§7.7 — the complete immutable executable package description.

    Separated concerns: CONTENT IDENTITY (what will execute), BUILD
    PROVENANCE (how it was built), VALIDATION EVIDENCE (what was observed —
    separate records), DEPLOYMENT AUTHORIZATION (where it may run — a
    separate service decision, §7.20)."""

    artifact_id: str
    connector_id: str
    connector_revision_id: Optional[str] = None
    artifact_format_version: str = ARTIFACT_FORMAT_VERSION
    entrypoint: str = "server.py:app"
    supported_transport: str = "stdio"
    supported_protocol_versions: List[str] = field(
        default_factory=lambda: ["2025-06-18"])
    # Content identity
    source_bundle: List[Dict[str, Any]] = field(default_factory=list)
    source_bundle_digest: str = ""
    tool_schema_digest: str = ""
    dependency_lock: Dict[str, Any] = field(default_factory=dict)
    dependency_lock_digest: str = ""
    runtime_image_digest: Optional[str] = None
    # Provenance
    provenance_record_id: Optional[str] = None
    spec_provenance: str = "unknown"
    # Capability statuses — INDEPENDENT fields (§7.2), never one boolean
    export_status: str = ExportStatus.NOT_EXPORTED.value
    build_status: str = "not_built"
    protocol_validation_status: str = ProtocolValidationStatus.NOT_RUN.value
    isolation_validation_status: str = IsolationValidationStatus.NOT_RUN.value
    deployment_status: str = DeploymentStatus.NOT_DEPLOYED.value
    # Mutable evidence lives in separate records; only pointers here
    attestation_ids: List[str] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0

    def content_digest(self) -> str:
        """§7.7: hash of the IMMUTABLE EXECUTABLE CONTENT — source bundle,
        tool schemas, dependency lock, entrypoint, transport. Excludes test
        evidence, export/deploy flags, and timestamps (so recorded protocol
        tests stay bound to the exact content they validated)."""
        material = {
            "artifact_format_version": self.artifact_format_version,
            "entrypoint": self.entrypoint,
            "supported_transport": self.supported_transport,
            "source_bundle": sorted(self.source_bundle,
                                    key=lambda e: e["relative_path"]),
            "source_bundle_digest": self.source_bundle_digest,
            "tool_schema_digest": self.tool_schema_digest,
            "dependency_lock": self.dependency_lock,
            "dependency_lock_digest": self.dependency_lock_digest,
            "runtime_image_digest": self.runtime_image_digest,
            "spec_provenance": self.spec_provenance,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    def to_row(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "connector_id": self.connector_id,
            "connector_revision_id": self.connector_revision_id,
            "artifact_format_version": self.artifact_format_version,
            "entrypoint": self.entrypoint,
            "supported_transport": self.supported_transport,
            "supported_protocol_versions_json": json.dumps(
                self.supported_protocol_versions),
            "source_bundle_json": json.dumps(self.source_bundle),
            "source_bundle_digest": self.source_bundle_digest,
            "tool_schema_digest": self.tool_schema_digest,
            "dependency_lock_json": json.dumps(self.dependency_lock),
            "dependency_lock_digest": self.dependency_lock_digest,
            "runtime_image_digest": self.runtime_image_digest,
            "provenance_record_id": self.provenance_record_id,
            "spec_provenance": self.spec_provenance,
            "export_status": self.export_status,
            "build_status": self.build_status,
            "protocol_validation_status": self.protocol_validation_status,
            "isolation_validation_status": self.isolation_validation_status,
            "deployment_status": self.deployment_status,
            "attestation_ids_json": json.dumps(self.attestation_ids),
            "content_digest": self.content_digest(),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }
