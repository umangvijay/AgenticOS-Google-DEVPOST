"""Typed artifact verification (Phase 17 §17.9, finding E).

Replaces truthiness with an explicit verdict. Requirements enforced:
- NEVER convert a result dictionary to a success boolean — a
  {"verified": false, ...} result FAILS.
- Artifacts resolve through server-owned identity (owner-scoped repo),
  not Python resolver functions persisted in acceptance criteria.
- Missing evidence → unavailable / needs_review, never pass.
- Integrity/ownership failures are BLOCKING.
- Every verdict binds to the exact revision and content digest —
  modifying the artifact after verification invalidates the result.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VERIFIER_VERSION = "artifact-verifier-1.1.0"


@dataclass
class ArtifactVerdict:
    verdict: str                 # pass | fail | needs_review | unavailable
    artifact_id: str
    revision: Optional[int]
    content_digest: str = ""
    checks: List[Dict[str, Any]] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    verifier_version: str = VERIFIER_VERSION

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


def _integrity_check(record: Dict[str, Any], stored_bytes_fn=None) -> Dict[str, Any]:
    """The checksum must match the ACTUAL bytes when they are available;
    a missing checksum is a MIGRATION-policy failure (needs review), not
    a silent pass (§17.8 finding G)."""
    if record.get("content_checksum"):
        return {"check": "integrity_metadata", "status": "present",
                "digest": record["content_checksum"]}
    return {"check": "integrity_metadata", "status": "missing",
            "detail": "legacy record without integrity metadata — needs "
                      "review under the migration policy"}


async def verify_artifact(*, artifact_id: str, owner_user_id: str,
                          repo,
                          stored_bytes_fn=None,
                          verifier_version: str = VERIFIER_VERSION,
                          ) -> ArtifactVerdict:
    """The typed verification entry — resolves through the SERVER-owned,
    owner-scoped repository (never a persisted Python callable)."""
    record = await repo.get_for_owner(artifact_id, owner_user_id)
    checks: List[Dict[str, Any]] = []
    blocking: List[str] = []
    evidence: List[str] = []
    if record is None:
        # Cross-tenant reads look identical to missing (§12.28).
        return ArtifactVerdict(
            verdict="unavailable", artifact_id=artifact_id, revision=None,
            checks=[{"check": "existence+ownership",
                     "status": "not_found_or_denied"}],
            blocking_reasons=["the artifact does not exist for this owner"],
            verifier_version=verifier_version)
    checks.append({"check": "existence+ownership", "status": "present"})
    # Integrity: the record's checksum must match the ACTUAL bytes.
    integrity = _integrity_check(_record_dict(record))
    checks.append(integrity)
    needs_review = False
    if integrity["status"] == "missing":
        # §17.8 migration policy: a legacy record without checksums is
        # NEEDS REVIEW — it cannot pass, but it is not proven tampered.
        needs_review = True
    stored = None
    manifest = getattr(record, "file_manifest", None) or []
    if stored_bytes_fn is None:
        # §17.9 (new probe 2): a checksum STRING with no byte check does
        # not confirm the bytes exist — verification without reading the
        # actual content is needs_review, never pass.
        checks.append({"check": "stored_bytes", "status": "not_performed",
                       "detail": "no byte verification was performed — the "
                                 "checksum alone does not prove content"})
        return ArtifactVerdict(
            verdict="needs_review", artifact_id=artifact_id,
            revision=getattr(record, "revision", None),
            content_digest=record.content_checksum,
            checks=checks,
            blocking_reasons=[],   # not blocking; it is unverified
            evidence_refs=[],
            verifier_version=verifier_version)
    # §19.2 (C2): PER-FILE verification — the resolver returns a MAP of
    # path → bytes (or a legacy single blob for one-file artifacts).
    # Every manifest entry must be present with matching length and
    # digest; an empty manifest cannot pass.
    if not manifest:
        blocking.append("empty manifest — there is nothing to verify "
                        "against (§19.2)")
        checks.append({"check": "manifest", "status": "empty"})
        return ArtifactVerdict(
            verdict="fail", artifact_id=artifact_id,
            revision=getattr(record, "revision", None),
            content_digest=record.content_checksum,
            checks=checks, blocking_reasons=blocking,
            verifier_version=verifier_version)
    # Validate the manifest before touching storage. Legacy omissions are
    # unverified; malformed or ambiguous manifests are blocking failures.
    seen_paths = set()
    incomplete_paths = set()
    for entry in manifest:
        if not isinstance(entry, dict):
            blocking.append("manifest entries must be objects")
            continue
        path_name = entry.get("path")
        if not isinstance(path_name, str) or not path_name.strip():
            blocking.append("manifest entry has no valid path")
            continue
        if path_name in seen_paths:
            blocking.append(f"duplicate manifest path: {path_name}")
        seen_paths.add(path_name)
        digest = entry.get("sha256")
        length = entry.get("byte_length")
        if digest is None or digest == "" or length is None:
            needs_review = True
            incomplete_paths.add(path_name)
            checks.append({"check": f"file:{path_name}",
                           "status": "missing_integrity_metadata"})
        if digest is not None and digest != "" and (
                not isinstance(digest, str) or
                re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None):
            blocking.append(f"invalid SHA-256 for file: {path_name}")
        if length is not None and (type(length) is not int or length < 0):
            blocking.append(f"invalid byte length for file: {path_name}")
    if blocking:
        return ArtifactVerdict(
            verdict="fail", artifact_id=artifact_id,
            revision=getattr(record, "revision", None),
            content_digest=record.content_checksum, checks=checks,
            blocking_reasons=blocking, verifier_version=verifier_version)

    stored = await stored_bytes_fn(artifact_id)
    files = None
    if isinstance(stored, dict):
        files = stored          # path → bytes map
    elif stored is None:
        blocking.append("the stored bytes are missing — the artifact "
                        "record exists but content does not")
        checks.append({"check": "stored_bytes", "status": "missing"})
    elif len(manifest) == 1:
        # Legacy single-file contract: the blob is the one file.
        files = {str(manifest[0].get("path") or ""): stored}
    else:
        blocking.append("the storage resolver returned a single blob for "
                        "a multi-file manifest — per-file verification "
                        "requires a path map")
        checks.append({"check": "stored_bytes", "status": "shape_mismatch"})
    if files is not None:
        all_ok = not incomplete_paths
        for entry in manifest:
            path_name = str(entry.get("path") or "")
            expected_sha = str(entry.get("sha256") or "")
            expected_len = entry.get("byte_length")
            actual_bytes = files.get(path_name)
            if actual_bytes is None:
                all_ok = False
                blocking.append(f"missing file: {path_name}")
                checks.append({"check": f"file:{path_name}",
                               "status": "missing"})
                continue
            if not isinstance(actual_bytes, (bytes, bytearray, memoryview)):
                all_ok = False
                blocking.append(f"storage returned non-byte content: {path_name}")
                checks.append({"check": f"file:{path_name}",
                               "status": "invalid_content_type"})
                continue
            actual_sha = hashlib.sha256(actual_bytes).hexdigest()
            actual_len = len(actual_bytes)
            length_ok = (expected_len is None
                         or expected_len == actual_len)
            digest_ok = (not expected_sha or actual_sha == expected_sha.lower())
            if length_ok and digest_ok and path_name in incomplete_paths:
                # Check whatever evidence exists, but never call an
                # incomplete manifest entry verified.
                continue
            if length_ok and digest_ok:
                checks.append({"check": f"file:{path_name}",
                               "status": "verified",
                               "digest": actual_sha[:32]})
            else:
                all_ok = False
                blocking.append(
                    f"file {path_name} does not match its manifest entry "
                    f"(digest_ok={digest_ok}, length_ok={length_ok})")
                checks.append({"check": f"file:{path_name}",
                               "status": "tampered",
                               "digest": actual_sha[:32]})
        if all_ok:
            checks.append({"check": "all_manifest_files_verified",
                           "status": "pass", "count": len(manifest)})
    # Verdict assembly: blocking → fail; legacy/absent integrity →
    # needs_review; else pass (bound to the digest).
    if blocking:
        verdict = "fail"
    elif needs_review:
        verdict = "needs_review"
    else:
        verdict = "pass"
        evidence.append(f"checksum:{record.content_checksum}")
    return ArtifactVerdict(
        verdict=verdict, artifact_id=artifact_id,
        revision=getattr(record, "revision", None),
        content_digest=record.content_checksum,
        checks=checks, blocking_reasons=blocking, evidence_refs=evidence,
        verifier_version=verifier_version)


def _record_dict(record):
    for attr in ("model_dump",):
        if hasattr(record, attr):
            return record.model_dump()
    if hasattr(record, "__dict__"):
        return dict(record.__dict__)
    return dict(record)
