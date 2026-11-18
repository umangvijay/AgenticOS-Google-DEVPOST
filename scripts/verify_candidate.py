#!/usr/bin/env python3
"""Phase 24 §24.1 — verify a candidate manifest from a SECOND checkout.

Extracts the self-contained archive in a fresh temporary directory,
then recomputes the source fingerprint and compares it to the manifest. A mismatch names the differing files.

Usage:
    .venv/bin/python scripts/verify_candidate.py <candidate-id>
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import sys
import io
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "docs/evidence/candidates"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(files: dict) -> str:
    """§25.1: the SHARED fingerprint function."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from source_identity import fingerprint_from_hashes
    return fingerprint_from_hashes(files)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reconstruct_candidate(cand_dir: Path, work: Path) -> dict:
    """Validate and extract exactly the bytes a release build will consume.

    The destination must be empty. Callers must not execute candidate code
    before this function returns. Archive bytes are read once, so a source
    archive replaced during verification cannot change the extracted input.
    """
    if work.is_symlink() or (work.exists() and any(work.iterdir())):
        raise ValueError("Candidate destination must be empty")
    manifest = json.loads((cand_dir / "manifest.json").read_text(),
                          object_pairs_hook=_unique_object)
    file_hashes = json.loads((cand_dir / "file-hashes.json").read_text(),
                             object_pairs_hook=_unique_object)
    expected = manifest["verification"]["expected_content_sha256"]

    # §29.3: the manifest is SELF-CONSISTENT before anything is trusted —
    # the frozen per-file hashes must independently reproduce the
    # expected fingerprint. Without this, a manifest whose file hashes
    # and expected fingerprint were edited TOGETHER would verify a
    # candidate that was never frozen.
    manifest_fp = fingerprint(file_hashes)
    if manifest_fp != expected:
        raise ValueError("MANIFEST INCONSISTENT: file hashes differ from expected fingerprint")
    if manifest.get("source", {}).get("content_sha256", expected) != expected:
        raise ValueError("MANIFEST INCONSISTENT: source and verification identities differ")

    if manifest["source_archive"]["path"] != "source-archive.tar.gz":
        raise ValueError("Unexpected candidate archive path")
    archive = cand_dir / "source-archive.tar.gz"

    # §25.1: the archive itself must be intact before anything else.
    archive_bytes = archive.read_bytes()
    actual_archive_sha = hashlib.sha256(archive_bytes).hexdigest()
    if actual_archive_sha != manifest["source_archive"]["sha256"]:
        raise ValueError("ARCHIVE CORRUPTED: archive hash differs from manifest")

    # Validate the complete archive before extracting any bytes. Manifest
    # entries and archive members must be identical regular, relative files.
    from pathlib import PurePosixPath
    mismatched, missing, recomputed = [], [], {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        for member in members:
            parts = PurePosixPath(member.name)
            if (parts.is_absolute() or ".." in parts.parts
                    or str(parts) != member.name or not member.isfile()):
                raise ValueError("UNSAFE ARCHIVE MEMBER")
        if len(names) != len(set(names)) or set(names) != set(file_hashes):
            raise ValueError("ARCHIVE FILE SET DIFFERS FROM MANIFEST")
        work.mkdir(parents=True, exist_ok=True)
        tar.extractall(work, members=members, filter="data")
    for name, meta in file_hashes.items():
        p = work / name
        if not p.is_file():
            missing.append(name)
            continue
        actual = {"sha256": sha256_file(p),
                  "mode": oct(p.stat().st_mode & 0o777)}
        if actual != {"sha256": meta["sha256"], "mode": meta["mode"]}:
            mismatched.append(name)
        recomputed[name] = actual
    fp = fingerprint(recomputed)

    if fp != expected or missing or mismatched:
        raise ValueError("NOT RECONSTRUCTIBLE: source content or mode differs")
    return manifest


def main() -> int:
    if len(sys.argv) != 2 or Path(sys.argv[1]).name != sys.argv[1] or sys.argv[1] in (".", ".."):
        print("usage: verify_candidate.py <candidate-id>")
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="agentos-verify-") as directory:
            manifest = reconstruct_candidate(CANDIDATES / sys.argv[1], Path(directory))
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError) as exc:
        print(f"RESULT: {exc}")
        return 1
    print(f"── fingerprint (reconstructed): {manifest['verification']['expected_content_sha256']}")
    print("RESULT: RECONSTRUCTIBLE — built from the self-contained "
          "archive without the original tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
