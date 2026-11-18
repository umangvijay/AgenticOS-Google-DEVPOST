#!/usr/bin/env python3
"""Phase 24 §24.1 — freeze a reconstructible candidate.

Produces docs/evidence/candidates/<candidate-id>/ containing:
  manifest.json      commit, source fingerprint, target profile, resolved
                     build inputs (pip freeze, package-lock digest), image
                     digests, enabled features, secret REFERENCES (names
                     only), and the reviewed inclusion rules
  file-hashes.json   sha256 + mode for EVERY included source file
                     (tracked + intended untracked implementation)

Excluded by rule: generated evidence, local data, logs, virtualenvs,
build outputs, caches, secret VALUES, and lockfiles' transitive noise.
The manifest is what a second checkout replays to verify
reconstructibility (scripts/verify_candidate.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/evidence/candidates"

sys.path.insert(0, str(ROOT / "scripts"))
from source_identity import (EXCLUDED_DIRS, EXCLUDED_PREFIXES,
                             EXCLUDED_SUFFIXES, EXCLUDED_NAMES, IDENTITY_VERSION)


def git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=ROOT).stdout.strip()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def included_files() -> list[str]:
    """§25.1: the SHARED file-set implementation — the freezer no longer
    maintains its own exclusion rules."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from source_identity import source_file_set
    return source_file_set(ROOT)


def main() -> int:
    files = included_files()
    tracked = set(git("ls-files", "-z").split("\0"))
    file_hashes = {}
    for name in files:
        p = ROOT / name
        file_hashes[name] = {
            "sha256": sha256_file(p),
            "mode": oct(p.stat().st_mode & 0o777),
            "tracked": name in tracked,
        }
    # §25.1: the SHARED fingerprint function — identical to the
    # qualifier's and the verifier's by construction.
    from source_identity import fingerprint_from_hashes
    content_sha256 = fingerprint_from_hashes(file_hashes)

    pip_freeze = subprocess.run(
        [str(ROOT / ".venv/bin/pip"), "freeze"], capture_output=True,
        text=True).stdout
    lock_hash = None
    fe_lock = ROOT / "frontend/package-lock.json"
    if fe_lock.exists():
        lock_hash = sha256_file(fe_lock)

    images = {}
    for img in ("agentos-api", "agentos-worker", "agentos-frontend",
                "agentos-runner"):
        r = subprocess.run(["docker", "image", "inspect", img + ":latest", "--format", "{{.Id}}"],
                           capture_output=True, text=True)
        images[img] = r.stdout.strip().splitlines()[0] if r.stdout.strip() \
            else None

    candidate_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" \
        + content_sha256[:12]
    out_dir = OUT / candidate_id
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "file-hashes.json").write_text(
        json.dumps(file_hashes, indent=2, sort_keys=True))
    (out_dir / "requirements-frozen.txt").write_text(pip_freeze)

    # §25.1: a SELF-CONTAINED source archive — verification must be able
    # to reconstruct WITHOUT the original working tree (a manifest of
    # hashes alone cannot recover a file after the original copy drifts).
    import tarfile
    archive_path = out_dir / "source-archive.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        for name in sorted(file_hashes):
            tar.add(ROOT / name, arcname=name)

    archive_sha = sha256_file(archive_path)
    manifest = {
        "candidate_id": candidate_id,
        "source_archive": {
            "path": "source-archive.tar.gz",
            "sha256": archive_sha},
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "manifest_version": 2,
        "source": {
            "commit": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty_files": len(git("status", "--porcelain").splitlines()),
            "content_sha256": content_sha256,
            "identity_version": IDENTITY_VERSION,
            "included_files": len(files),
            "untracked_included": sum(
                1 for m in file_hashes.values() if not m["tracked"]),
            "inclusion_rules": {
                "excluded_dirs": sorted(EXCLUDED_DIRS),
                "excluded_prefixes": sorted(EXCLUDED_PREFIXES),
                "excluded_suffixes": list(EXCLUDED_SUFFIXES),
                "excluded_names": sorted(EXCLUDED_NAMES),
                "dotenv_policy": "exclude .env.* except .example/.sample/.template",
                "symlink_policy": "reject",
                "note": "generated evidence, local data, logs, virtualenvs, "
                        "build outputs, caches, and secret VALUES are "
                        "excluded; secret references only below"},
        },
        "build_inputs": {
            "python": subprocess.run(
                [str(ROOT / ".venv/bin/python"), "--version"],
                capture_output=True, text=True).stdout.strip(),
            "requirements_frozen": "requirements-frozen.txt",
            "requirements_pinned": "requirements.txt",
            "frontend_lock_sha256": lock_hash,
            "node": subprocess.run(["node", "--version"],
                                   capture_output=True,
                                   text=True).stdout.strip(),
            "images": images,
            "images_source_verified": False,
            "container_tags_note": "observed local image IDs only; source-to-image "
                                   "build provenance and target deployment remain separate qualification gates",
        },
        "target_profile": {
            "profile": os.environ.get("TARGET_PROFILE",
                                      "local-durable-postgres"),
            "storage": "PostgreSQL 16; required migrations listed below (not an applied-state claim)",
            "required_migrations": sorted(p.name for p in
                (ROOT / "backend/repositories/postgres/migrations").glob("*.sql")),
            "transport": "NATS JetStream + Postgres transactional outbox",
            "artifact_store": "shared local volume (agentos-artifacts)",
            "providers": ["ollama-local", "vertex", "bedrock", "azure"],
            "browser_worker": "Chromium (compose browser profile)",
            "runner": "isolated sibling containers (agentos-runner image)",
            "replicas": {"api": 1, "worker": 1, "browser": 0, "runner": 0},
            "requested_clouds": ["gcp", "aws", "azure"],
            "cloud_evidence": "pending separately for each cloud",
        },
        "secret_references": [
            {"name": "GEMINI_API_KEY / GOOGLE_API_KEY",
             "present": bool(os.environ.get("GOOGLE_API_KEY")),
             "storage": "env / .env (never committed)"},
            {"name": "AWS_BEARER_TOKEN_BEDROCK",
             "present": bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))},
            {"name": "GROQ_API_KEY",
             "present": bool(os.environ.get("GROQ_API_KEY"))},
            {"name": "backend/security/keys (JWT + vault material)",
             "present": (ROOT / "backend/security/keys").exists(),
             "storage": "local key files (excluded from the manifest)"},
        ],
        "verification": {
            "method": "scripts/verify_candidate.py extracts the "
                      "self-contained source archive into a UNIQUE "
                      "temporary directory and recomputes the shared "
                      "fingerprint — the original working tree is never "
                      "read",
            "expected_content_sha256": content_sha256,
            "shared_implementation": "scripts/source_identity.py",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"candidate frozen: {candidate_id}")
    print(f"  files: {len(files)} "
          f"({manifest['source']['untracked_included']} untracked included)")
    print(f"  content_sha256: {content_sha256}")
    print(f"  manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
