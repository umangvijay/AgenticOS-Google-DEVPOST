#!/usr/bin/env python3
"""Phase 25 §25.1 — THE single source-fingerprint implementation.

Every producer and consumer of release identity — the candidate freezer,
the checkout verifier, the qualification harness, and the journey
evidence producer — MUST call `source_fingerprint()` here. Divergent
hash calculations are how a release ends up bound to three different
identities (the Phase 24 audit found `ce01b62a…` / `1dcbe89d…` /
`998f97dc…` / `6d232fbd…` for the same source).

Canonical algorithm (frozen):
  1. file set   = git ls-files (tracked) ∪ git ls-files --others
                  --exclude-standard (untracked), minus EXCLUDED
  2. per file   = sha256(raw bytes) + octal permission string
  3. fingerprint = sha256 over, in sorted-name order:
                     name + b"\\0" + digest-bytes + mode-ascii + b"\\0"

Exclusions (frozen): generated evidence, local data, logs, caches,
virtualenvs, build outputs, secret VALUES. Changing this list changes
every future fingerprint — do it only through a reviewed manifest
version bump.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

EXCLUDED_PREFIXES = (
    "docs/evidence/", "data/", "backend/security/keys/",
    "frontend/playwright/", "frontend/.next/", "frontend/test-results/",
    "frontend/playwright-report/",
)
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "node_modules", ".next", "__pycache__",
    "data", "test-results", "playwright-report", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".zcode",
}
EXCLUDED_SUFFIXES = (".log", ".pyc", ".sqlite3", ".db", ".pem", ".key")
EXCLUDED_NAMES = {".env", ".env.local", ".DS_Store"}
IDENTITY_VERSION = 2


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=root).stdout.strip()


def is_excluded(name: str) -> bool:
    if any(name.startswith(px) for px in EXCLUDED_PREFIXES):
        return True
    if set(Path(name).parts) & EXCLUDED_DIRS:
        return True
    if Path(name).name in EXCLUDED_NAMES:
        return True
    if Path(name).name.startswith(".env.") and not Path(name).name.endswith((".example", ".sample", ".template")):
        return True
    return name.endswith(EXCLUDED_SUFFIXES)


def source_file_set(root: Path) -> list[str]:
    """The canonical included-file list (tracked + untracked, excluded
    removed, existing files only). Outside a git repository (e.g. a
    verification sandbox) the file set comes from a directory walk with
    the same exclusions."""
    tracked = [p for p in _git(root, "ls-files", "-z").split("\0") if p]
    untracked = [p for p in
                 _git(root, "ls-files", "-z", "--others",
                      "--exclude-standard").split("\0") if p]
    names = sorted(set(tracked) | set(untracked))
    if not names and not (root / ".git").exists():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            names.append(str(path.relative_to(root)))
    out = []
    for name in names:
        if is_excluded(name):
            continue
        path = root / name
        relative_parts = Path(name).parts
        if any(root.joinpath(*relative_parts[:i]).is_symlink() for i in range(1, len(relative_parts) + 1)):
            raise ValueError(f"source candidate must contain regular files, not symlinks: {name}")
        if (root / name).is_file():
            out.append(name)
    return out


def file_hash(path: Path) -> dict:
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": oct(path.stat().st_mode & 0o777)}


def file_hashes(root: Path) -> dict:
    out = {}
    for name in source_file_set(root):
        out[name] = file_hash(root / name)
    return out


def fingerprint_from_hashes(file_hashes: dict) -> str:
    """The canonical fingerprint over ALREADY-HASHED files. Both the
    freezer and the qualifier feed this function; there is exactly one
    algorithm."""
    digest = hashlib.sha256()
    for name in sorted(file_hashes):
        meta = file_hashes[name]
        digest.update(name.encode() + b"\0")
        digest.update(bytes.fromhex(meta["sha256"]))
        digest.update(meta["mode"].encode())
        digest.update(b"\0")
    return digest.hexdigest()


def source_fingerprint(root: Path) -> tuple[str, dict]:
    """Convenience: (fingerprint, per-file hashes) for a tree."""
    hashes = file_hashes(root)
    return fingerprint_from_hashes(hashes), hashes


def git_identity(root: Path) -> dict:
    """The qualifier's identity record, from the SAME implementation."""
    import datetime as dt
    fp, _ = source_fingerprint(root)
    return {
        "commit": _git(root, "rev-parse", "HEAD") or "no-git",
        "dirty_files": len(_git(root, "status", "--porcelain")
                           .splitlines()),
        "content_sha256": fp,
        "identity_version": IDENTITY_VERSION,
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "recorded_at": dt.datetime.now().astimezone()
        .isoformat(timespec="seconds"),
    }
