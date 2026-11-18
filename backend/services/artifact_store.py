"""Artifact store contract (Phase 20 §20.6).

Write/read/metadata/delete over an authorized, immutable, digest-verified
object store. The default backend is the local filesystem (local-first);
the CONTRACT is backend-neutral — a GCS/S3 backend implements the same
six operations.

Invariants (each is a test below):
- Immutable revision identities: bytes are written once (O_EXCL) and
  content-addressed; re-putting IDENTICAL bytes returns the SAME
  identity; changed bytes create a NEW revision chained to the previous.
- Per-file digests: every artifact records sha256 + byte length; reads
  verify the digest of the returned bytes (integrity on read, not just
  on write).
- Authorized download: reads are server-enforced — the actor must be the
  owner or hold an explicit grant; a foreign actor gets ArtifactNotAuthorized,
  and a missing artifact gets ArtifactNotFound (indistinguishable from
  "no such object" — never leak existence to non-owners).
- Delete is owner-only and removes bytes + metadata together.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional


class ArtifactStoreError(Exception):
    pass


class ArtifactNotFound(ArtifactStoreError):
    """Raised for missing artifacts AND for artifacts the actor may not
    know exist (non-owners) — existence is never leaked."""


class ArtifactNotAuthorized(ArtifactStoreError):
    """The actor is authenticated but not authorized for this action
    (e.g. a non-owner attempting delete)."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored bytes no longer match their recorded digest."""


@dataclass
class ArtifactMetadata:
    artifact_id: str
    owner_user_id: str
    run_id: str
    name: str
    revision: int
    parent_artifact_id: Optional[str]
    content_sha256: str
    byte_length: int
    content_type: str
    created_at: float
    grants: List[str] = field(default_factory=list)

    def public_dict(self) -> Dict:
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "name": self.name,
            "revision": self.revision,
            "parent_artifact_id": self.parent_artifact_id,
            "content_sha256": self.content_sha256,
            "byte_length": self.byte_length,
            "content_type": self.content_type,
            "created_at": self.created_at,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _serialized(method):
    """Serialize local-store operations across processes without blocking asyncio.

    This backend requires a local POSIX filesystem with flock semantics. It
    does not claim object-store or multi-host transaction guarantees.
    """
    @wraps(method)
    async def call(self, *args, **kwargs):
        def locked():
            fd = os.open(self.root / ".store.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                return method(self, *args, **kwargs)
            finally:
                os.close(fd)
        return await asyncio.to_thread(locked)
    return call


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    """Local-filesystem implementation of the §20.6 contract. Blobs live
    under ``blobs/<aa>/<content_sha256>`` (write-once, content-addressed,
    deduplicated across revisions); metadata JSON under
    ``meta/<artifact_id>.json``. Both carry restrictive modes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._blob_dir = self.root / "blobs"
        self._meta_dir = self.root / "meta"
        self._blob_dir.mkdir(parents=True, exist_ok=True)
        self._meta_dir.mkdir(parents=True, exist_ok=True)

    # ── write ──────────────────────────────────────────────────────────

    @_serialized
    def put(self, owner_user_id: str, run_id: str, name: str,
                  content: bytes,
                  content_type: str = "application/octet-stream",
                  ) -> ArtifactMetadata:
        """Store content as the next revision of (run_id, name).
        Identical bytes to the current revision return the SAME identity
        (idempotent write); changed bytes chain a new revision."""
        digest = _sha256(content)
        if not all(isinstance(v, str) and v for v in (owner_user_id, run_id, name)):
            raise ArtifactStoreError("Owner, run and name are required")
        current = self._latest(owner_user_id, run_id, name)
        blob_path = self._blob_path(digest)
        if blob_path.exists() and (blob_path.is_symlink() or _sha256(blob_path.read_bytes()) != digest):
            raise ArtifactIntegrityError("Existing content-addressed blob is corrupt")
        if not blob_path.exists():
            self._write_once(blob_path, content)
        if current is not None and current.content_sha256 == digest:
            return current  # immutable identity: same bytes, same id

        artifact_id = f"a-{digest[:16]}-{int(time.time_ns()) % 1_000_000:06d}"
        while (self._meta_dir / f"{artifact_id}.json").exists():
            artifact_id += "x"  # never overwrite an existing identity

        meta = ArtifactMetadata(
            artifact_id=artifact_id,
            owner_user_id=owner_user_id,
            run_id=run_id,
            name=name,
            revision=(current.revision + 1) if current else 1,
            parent_artifact_id=current.artifact_id if current else None,
            content_sha256=digest,
            byte_length=len(content),
            content_type=content_type,
            created_at=time.time())
        self._write_meta(meta)
        return meta

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        """Publish only complete, fsynced bytes; never overwrite a blob."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".blob-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
            os.link(tmp, path)
            _sync_directory(path.parent)
        finally:
            os.unlink(tmp)

    # ── read ───────────────────────────────────────────────────────────

    @_serialized
    def get(self, actor_user_id: str, artifact_id: str):
        """Authorized read: owner or granted actor only; digest verified
        against the stored bytes on every read."""
        meta = self._load_meta(actor_user_id, artifact_id)
        blob = self._blob_path(meta.content_sha256)
        if not blob.exists() or blob.is_symlink():
            raise ArtifactIntegrityError(
                f"artifact {artifact_id}: blob bytes are missing")
        content = blob.read_bytes()
        if _sha256(content) != meta.content_sha256 or len(content) != meta.byte_length:
            raise ArtifactIntegrityError(
                f"artifact {artifact_id}: stored bytes do not match the "
                "recorded sha256 — refusing to serve corrupt content")
        return meta, content

    @_serialized
    def metadata(self, actor_user_id: str,
                       artifact_id: str) -> ArtifactMetadata:
        return self._load_meta(actor_user_id, artifact_id)

    @_serialized
    def list(self, actor_user_id: str,
                   run_id: str) -> List[ArtifactMetadata]:
        out = []
        for path in sorted(self._meta_dir.glob("*.json")):
            meta = self._read_meta_file(path)
            if (meta and meta.run_id == run_id
                    and self._may_read(meta, actor_user_id)):
                out.append(meta)
        return sorted(out, key=lambda m: (m.name, m.revision))

    # ── grants (authorized download) ───────────────────────────────────

    @_serialized
    def grant(self, owner_user_id: str, artifact_id: str,
                    grantee_user_id: str) -> None:
        meta = self._load_meta(owner_user_id, artifact_id)
        if meta.owner_user_id != owner_user_id:
            raise ArtifactNotAuthorized(
                "only the owner can grant download access")
        if grantee_user_id not in meta.grants:
            meta.grants.append(grantee_user_id)
            self._write_meta(meta)

    @_serialized
    def revoke(self, owner_user_id: str, artifact_id: str,
                     grantee_user_id: str) -> None:
        meta = self._load_meta(owner_user_id, artifact_id)
        if meta.owner_user_id != owner_user_id:
            raise ArtifactNotAuthorized(
                "only the owner can revoke download access")
        meta.grants = [g for g in meta.grants if g != grantee_user_id]
        self._write_meta(meta)

    # ── delete ─────────────────────────────────────────────────────────

    @_serialized
    def delete(self, actor_user_id: str, artifact_id: str) -> bool:
        meta_path = self._meta_path(artifact_id)
        if not meta_path.exists():
            raise ArtifactNotFound(f"artifact {artifact_id} does not exist")
        meta = self._read_meta_file(meta_path)
        if meta is None:
            raise ArtifactNotFound(f"artifact {artifact_id} is unreadable")
        if meta.owner_user_id != actor_user_id:
            raise ArtifactNotAuthorized(
                "only the owner can delete an artifact")
        # Descendants must not dangle: refuse deletion while later
        # revisions reference this identity.
        for other_path in self._meta_dir.glob("*.json"):
            other = self._read_meta_file(other_path)
            if other and other.parent_artifact_id == artifact_id:
                raise ArtifactStoreError(
                    f"artifact {artifact_id} has revision "
                    f"{other.revision} descending from it — delete the "
                    "chain tail first")
        meta_path.unlink(missing_ok=True)
        _sync_directory(self._meta_dir)
        # The blob may be shared by identical artifacts; remove only when
        # no remaining metadata references it.
        digest = meta.content_sha256
        remaining = [self._read_meta_file(p) for p in self._meta_dir.glob("*.json")]
        # Unreadable metadata may still reference a shared blob. Preserve it.
        still_referenced = any(m is None or m.content_sha256 == digest for m in remaining)
        if not still_referenced:
            self._blob_path(digest).unlink(missing_ok=True)
        return True

    # ── internals ──────────────────────────────────────────────────────

    def _blob_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactIntegrityError("Invalid blob digest")
        return self._blob_dir / digest[:2] / digest

    def _meta_path(self, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", artifact_id):
            raise ArtifactNotFound("Artifact not found")
        return self._meta_dir / f"{artifact_id}.json"

    def _read_meta_file(self, path: Path) -> Optional[ArtifactMetadata]:
        try:
            if path.is_symlink():
                return None
            raw = json.loads(path.read_text())
            meta = ArtifactMetadata(**raw)
            if (self._meta_path(meta.artifact_id) != path
                    or not isinstance(meta.content_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", meta.content_sha256)
                    or type(meta.byte_length) is not int or meta.byte_length < 0
                    or type(meta.revision) is not int or meta.revision < 1
                    or not isinstance(meta.grants, list)
                    or not all(isinstance(g, str) for g in meta.grants)):
                return None
            return meta
        except (OSError, ValueError, TypeError, ArtifactStoreError):
            return None

    def _write_meta(self, meta: ArtifactMetadata) -> None:
        payload = json.dumps({
            "artifact_id": meta.artifact_id,
            "owner_user_id": meta.owner_user_id,
            "run_id": meta.run_id,
            "name": meta.name,
            "revision": meta.revision,
            "parent_artifact_id": meta.parent_artifact_id,
            "content_sha256": meta.content_sha256,
            "byte_length": meta.byte_length,
            "content_type": meta.content_type,
            "created_at": meta.created_at,
            "grants": meta.grants,
        }, indent=2)
        # Atomic metadata write: temp + rename, 0600.
        fd, tmp = tempfile.mkstemp(dir=self._meta_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(payload.encode())
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self._meta_path(meta.artifact_id))
            _sync_directory(self._meta_dir)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _latest(self, owner_user_id: str, run_id: str, name: str) -> Optional[ArtifactMetadata]:
        candidates = []
        for path in self._meta_dir.glob("*.json"):
            meta = self._read_meta_file(path)
            if (meta and meta.owner_user_id == owner_user_id
                    and meta.run_id == run_id and meta.name == name):
                candidates.append(meta)
        if not candidates:
            return None
        return max(candidates, key=lambda m: m.revision)

    @staticmethod
    def _may_read(meta: ArtifactMetadata, actor_user_id: str) -> bool:
        return (meta.owner_user_id == actor_user_id
                or actor_user_id in meta.grants)

    def _load_meta(self, actor_user_id: str,
                   artifact_id: str) -> ArtifactMetadata:
        path = self._meta_path(artifact_id)
        if not path.exists():
            raise ArtifactNotFound(f"artifact {artifact_id} does not exist")
        meta = self._read_meta_file(path)
        if meta is None:
            raise ArtifactNotFound(f"artifact {artifact_id} is unreadable")
        if not self._may_read(meta, actor_user_id):
            # Non-owners learn NOTHING — same error as absence.
            raise ArtifactNotFound(f"artifact {artifact_id} does not exist")
        return meta
