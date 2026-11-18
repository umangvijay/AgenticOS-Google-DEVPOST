"""Unified artifact service (Phase 7 §7.8/§7.9).

ONE flow connecting standalone_packaging (the artifact description + test
gate) and portable_server (the actual ZIP): select a connector revision and
operation subset → generate the source bundle → produce the dependency lock
→ compute executable identities → store the immutable artifact → generate
the download FROM THOSE STORED BYTES → validate the same artifact → deploy
the same digest. Source is never regenerated during deployment.

§7.9: the dependency lock is a REAL lock — Python runtime + platform, direct
AND transitive dependencies, exact distributions with integrity hashes,
architecture, package source, resolver versions. Hash-checked installation
per pip's secure-install guidance. Clean builds cannot silently resolve
additional dependencies; changed package files fail integrity verification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import zipfile
import io
import re
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.models.mcp_artifact import (
    MCPArtifact,
    ExportStatus,
    source_bundle_entry,
    source_bundle_digest,
    dependency_lock_digest,
    validate_bundle_path,
)

logger = logging.getLogger(__name__)

MAX_BUNDLE_BYTES = 2_000_000
MAX_EXTRACTED_BYTES = 8_000_000
MAX_ENTRIES = 500


# ── §7.9: dependency lock ──────────────────────────────────────────────

@dataclass
class DependencyLock:
    """§7.9 — the complete lock: exact distributions with integrity hashes."""
    python_version: str
    platform: str                        # e.g. manylinux2014_x86_64
    architecture: str                    # e.g. x86_64
    packages: List[Dict[str, Any]] = field(default_factory=list)
    # package: {"name", "version", "distribution_hash": "sha256:...",
    #           "source": "pypi", "transitive": bool}
    resolver_version: str = "pip"
    hash_check_mode: bool = True

    def validate(self) -> None:
        if self.hash_check_mode is not True:
            raise ValueError("Dependency locks must enforce hash checking")
        seen = set()
        for package in self.packages:
            name = package.get("name", "")
            version = package.get("version", "")
            digest = package.get("distribution_hash", "")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                raise ValueError("Invalid locked package name")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in seen:
                raise ValueError(f"Duplicate locked package: {name}")
            seen.add(normalized)
            if (not isinstance(version, str)
                    or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version)):
                raise ValueError(f"Dependency {name} requires an exact version")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError(f"Dependency {name} requires a full SHA-256 hash")

    def requirements(self) -> str:
        self.validate()
        return "".join(
            f"{p['name']}=={p['version']} --hash={p['distribution_hash']}\n"
            for p in sorted(self.packages, key=lambda p: p["name"]))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_format": "p7.v1",
            "python_version": self.python_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "packages": sorted(self.packages, key=lambda p: p["name"]),
            "resolver_version": self.resolver_version,
            "hash_check_mode": self.hash_check_mode,
        }

    def digest(self) -> str:
        return dependency_lock_digest(self.to_dict())

    def verify_integrity(self, installed: Dict[str, str]) -> Tuple[bool, List[str]]:
        """§7.9 acceptance: a changed package file fails integrity
        verification — every locked package must be present with the EXACT
        recorded hash. Returns (ok, violations)."""
        try:
            self.validate()
        except ValueError as exc:
            return False, [str(exc)]
        violations = []
        locked = {p["name"]: p for p in self.packages}
        for name, got in installed.items():
            if name not in locked:
                violations.append(f"{name}: not in lock (clean builds cannot "
                                  "silently resolve additional dependencies)")
                continue
            expected = locked[name].get("distribution_hash", "")
            if not isinstance(got, str) or got.removeprefix("sha256:") != expected.removeprefix("sha256:"):
                violations.append(f"{name}: hash mismatch")
        for name in locked:
            if name not in installed:
                violations.append(f"{name}: locked package missing")
        return (not violations), violations


def build_dependency_lock(python_version: str, platform: str, architecture: str,
                          direct: List[Dict[str, str]],
                          transitive: List[Dict[str, str]],
                          resolver_version: str = "pip") -> DependencyLock:
    """§7.9 build sequence steps 1-4: resolve in the isolated builder,
    download approved distributions, verify hashes, produce the complete
    lock. The caller (isolated builder) supplies verified entries."""
    packages = []
    for p in direct:
        packages.append({**p, "transitive": False})
    for p in transitive:
        packages.append({**p, "transitive": True})
    for p in packages:
        if "distribution_hash" not in p or not p["distribution_hash"]:
            raise ValueError(
                f"Dependency {p.get('name')} has no integrity hash — hash-"
                "checked installation requires hashes for ALL dependencies (§7.9)")
    lock = DependencyLock(python_version=python_version, platform=platform,
                          architecture=architecture, packages=packages,
                          resolver_version=resolver_version)
    lock.validate()
    return lock


# ── §7.8: the unified artifact service ────────────────────────────────

@dataclass
class BuiltArtifact:
    """The result of ONE packaging pass: the immutable artifact model plus
    the exact bytes for the portable export. Downloads, validation, and
    deployment all use THESE bytes — never regenerated."""
    artifact: MCPArtifact
    legacy_artifact: Dict[str, Any]       # standalone_packaging shape (test gate)
    export_zip: bytes                     # the actual portable ZIP
    export_manifest: Dict[str, Any]       # artifact-manifest.json contents


def package_artifact(
    *, connector_id: str, connector_revision_id: Optional[str],
    manifest: Dict[str, Any], tools: List[Dict[str, Any]],
    server_code: str, server_filename: str = "server.py",
    dependency_lock: DependencyLock, spec_provenance: str = "unknown",
    tool_schema_digest: str = "",
    extra_files: Optional[Dict[str, bytes]] = None,
    artifact_id: Optional[str] = None,
) -> BuiltArtifact:
    """§7.8 target flow steps 2-5: generate the source bundle, produce the
    lock, compute identities, store the immutable artifact. Nested input
    structures are COPIED at the packaging boundary (§7.7 requirement 1 —
    the mutable-nested-tool defect). The export ships marked UNTESTED."""
    import uuid as _uuid
    from datetime import datetime, timezone
    from backend.mcp.standalone_packaging import package_standalone_server

    now = int(datetime.now(timezone.utc).timestamp())
    dependency_lock.validate()
    validate_bundle_path(server_filename)
    reserved = {"tools.json", "README.md", "artifact-manifest.json",
                "requirements.lock", "dependency-lock.json"}
    if server_filename in reserved or not server_filename.endswith(".py"):
        raise ValueError("Entrypoint must be a non-reserved Python source file")
    extra_files = dict(extra_files or {})
    for path, content in extra_files.items():
        validate_bundle_path(path)
        if path in reserved or path == server_filename:
            raise ValueError(f"Reserved or duplicate bundle path: {path}")
        if not isinstance(content, bytes):
            raise ValueError("Extra files must contain bytes")
    # §7.7 req 1: deep-copy nested structures so later mutation of the
    # caller's objects cannot change packaged content.
    tools_copy = json.loads(json.dumps(tools, default=str))
    manifest_copy = json.loads(json.dumps(manifest, default=str))

    # Source bundle (§7.7): server.py + tools.json + declared extras.
    tools_json = json.dumps(tools_copy, indent=2, sort_keys=True).encode()
    entries = [
        source_bundle_entry(server_filename, server_code.encode(), "python"),
        source_bundle_entry("tools.json", tools_json, "json"),
    ]
    for path, content in sorted((extra_files or {}).items()):
        entries.append(source_bundle_entry(path, content))
    bundle_digest = source_bundle_digest(entries)
    lock_digest = dependency_lock.digest()

    # Legacy packaging shape (test gate, standalone_packaging.py) — kept for
    # existing consumers; the MCPArtifact model is the authority.
    legacy = package_standalone_server(
        manifest=manifest_copy, tools=tools_copy, server_code=server_code,
        spec_provenance=spec_provenance)
    legacy["dependency_lock"] = dependency_lock.to_dict()

    artifact = MCPArtifact(
        artifact_id=artifact_id or f"art_{_uuid.uuid4().hex[:24]}",
        connector_id=connector_id,
        connector_revision_id=connector_revision_id,
        entrypoint=f"{server_filename}:app",
        supported_transport="stdio",
        source_bundle=entries,
        source_bundle_digest=bundle_digest,
        tool_schema_digest=tool_schema_digest or legacy.get("manifest_digest", ""),
        dependency_lock=dependency_lock.to_dict(),
        dependency_lock_digest=lock_digest,
        spec_provenance=spec_provenance,
        export_status=ExportStatus.EXPORTED_UNTESTED.value,   # §7.2: untested
        created_at=now, updated_at=now,
    )

    export_manifest = {
        "artifact_id": artifact.artifact_id,
        "artifact_format_version": artifact.artifact_format_version,
        "content_digest": artifact.content_digest(),
        "source_bundle_digest": bundle_digest,
        "source_bundle": entries,
        "entrypoint": artifact.entrypoint,
        "tool_schema_digest": artifact.tool_schema_digest,
        "dependency_lock_digest": lock_digest,
        "dependency_lock": dependency_lock.to_dict(),
        "spec_provenance": spec_provenance,
        "export_status": artifact.export_status,
        "validation_status": "UNTESTED — this export has not been validated "
                             "in an isolated runtime; validation-summary.json "
                             "will accompany tested releases when present",
    }
    zip_bytes = _build_export_zip(server_filename, server_code, tools_json,
                                  export_manifest, dependency_lock, extra_files)
    return BuiltArtifact(artifact=artifact, legacy_artifact=legacy,
                         export_zip=zip_bytes, export_manifest=export_manifest)


def _build_export_zip(server_filename: str, server_code: str, tools_json: bytes,
                      export_manifest: Dict[str, Any],
                      lock: DependencyLock,
                      extra_files: Optional[Dict[str, bytes]] = None) -> bytes:
    """§7.8: generate the portable download FROM the packaged bytes. Zip
    entries sorted deterministically; bounded size; no compression surprises.
    Contents: server.py, tools.json, artifact-manifest.json,
    requirements.lock, README.md (untested status + setup + limits)."""
    buf = io.BytesIO()
    readme = (
        "# Standalone MCP Server (UNTESTED EXPORT)\n\n"
        "This export was generated by AgentOS and has NOT been validated in "
        "an isolated runtime. Review the source before running it.\n\n"
        "## Setup\n\n"
        f"1. Python {lock.python_version} ({lock.architecture}).\n"
        "2. Hash-checked install: `pip install --require-hashes -r requirements.lock`\n"
        "   (all distributions carry integrity hashes; source builds may run — "
        "review the lock first).\n"
        f"3. Run: `python {server_filename}` (stdio transport).\n\n"
        "## Permission model & limits\n\n"
        "- The server runs with YOUR credentials on YOUR machine — AgentOS "
        "exercises no control over an independent export.\n"
        "- AgentOS's validation summary (when present) describes behavior "
        "under AGENTOS's runner isolation, not yours.\n"
    )
    lock_json = json.dumps(lock.to_dict(), indent=2)
    entries = [
        ("README.md", readme.encode()),
        ("artifact-manifest.json",
         json.dumps(export_manifest, indent=2, sort_keys=True).encode()),
        ("requirements.lock", lock.requirements().encode()),
        ("dependency-lock.json", lock_json.encode()),
        ("tools.json", tools_json),
        (server_filename, server_code.encode()),
    ]
    entries.extend((extra_files or {}).items())
    if len(entries) > MAX_ENTRIES:
        raise ValueError("Export exceeds the file count limit")
    names = {name for name, _ in entries}
    for name in names:
        if any(parent in names for parent in _parents(name)):
            raise ValueError("Bundle contains a file/directory path conflict")
    total = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(entries):   # deterministic order
            if total + len(data) > MAX_BUNDLE_BYTES:
                raise ValueError("Export exceeds the bundle size limit")
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16   # normalized metadata (reproducible)
            zf.writestr(info, data)
            total += len(data)
    return buf.getvalue()


def _parents(name: str):
    parts = name.split("/")
    return ("/".join(parts[:i]) for i in range(1, len(parts)))


def verify_export_zip(zip_bytes: bytes) -> Tuple[bool, List[str]]:
    """§7.7 reqs 4-5: detect duplicate entries, bound size, reject paths
    escaping the archive root."""
    violations = []
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            if len(zf.infolist()) > MAX_ENTRIES:
                return False, ["archive entry count exceeds bound"]
            for info in zf.infolist():
                if info.filename in seen:
                    violations.append(f"duplicate entry: {info.filename}")
                seen.add(info.filename)
                try:
                    validate_bundle_path(info.filename)
                except ValueError:
                    violations.append(f"unsafe path: {info.filename}")
                if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
                    violations.append(f"non-regular file: {info.filename}")
                if info.flag_bits & 1:
                    violations.append("encrypted archives are not supported")
                total += info.file_size
                if total > MAX_EXTRACTED_BYTES:
                    violations.append("extracted size exceeds bound")
                    break
            if any(parent in seen for name in seen for parent in _parents(name)):
                violations.append("file/directory path conflict")
            if not violations:
                # Reading within the declared size bound verifies ZIP CRCs.
                for info in zf.infolist():
                    zf.read(info)
    except zipfile.BadZipFile as e:
        violations.append(f"corrupt archive: {e}")
    return (not violations), violations
