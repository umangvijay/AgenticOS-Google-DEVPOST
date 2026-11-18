#!/usr/bin/env python3
"""Publish current source onto Devpost history, without local phase-doc history.

Default: inspect the source manifest without network access.
Use --push to clone the destination, commit the source update, and push normally.
Remote-only files are retained, except phase Markdown and reviewed cleanup files. Local files,
the local Git index, branch, and origin are never modified by this script.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "https://github.com/umangvijay/AgenticOS-Google-DEVPOST.git"
# Reviewed obsolete files: propagate their local removal to an older checkout.
REMOVED_FILES = (
    "changes.md",
    "docs/CLEANUP_AUDIT.md",
    "frontend/test-layout.js",
    "vitest.config.ts",
    "ngrok.zip",
)


def git(*args: str, cwd: Path | None = None, **kwargs):
    # Terminal runs must not pause at diff/log output waiting for a pager exit.
    return subprocess.run(["git", "--no-pager", *args], cwd=cwd or ROOT, check=True, **kwargs)


def phase_markdown(name: str) -> bool:
    return name.lower().endswith(".md") and "phase" in name.lower()


def source_files() -> list[str]:
    raw = git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
              capture_output=True).stdout
    names = sorted(set(n for n in raw.decode().split("\0") if n))
    # --no-index also applies exclusions to already tracked PHASES.md.
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"], cwd=ROOT,
        input=("\0".join(names) + "\0").encode(), capture_output=True)
    if ignored.returncode not in (0, 1):
        raise RuntimeError(ignored.stderr.decode())
    excluded = set(ignored.stdout.decode().split("\0"))
    return [n for n in names if n not in excluded and not phase_markdown(n)
            and (ROOT / n).is_file()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    names = source_files()
    for name in names:
        if (ROOT / name).is_symlink():
            raise RuntimeError(f"Review source symlink before publishing: {name}")
    size = sum((ROOT / n).stat().st_size for n in names)
    print(f"Source manifest: {len(names)} files, {size / 1024**2:.1f} MiB", flush=True)
    print("Excluded: phase Markdown, ignored credentials, dependencies, builds, "
          "generated evidence and local logs/archives.", flush=True)
    if not args.push:
        print("Checked only. To publish: python3 scripts/publish_devpost.py --push")
        return
    # A fresh clone preserves the destination's history and avoids publishing
    # local historical commits which contain excluded planning documents.
    with tempfile.TemporaryDirectory(prefix="sandtable-publish-") as temp:
        dest = Path(temp) / "repo"
        git("clone", REMOTE, str(dest))
        git("rm", "--ignore-unmatch", "--", *REMOVED_FILES, cwd=dest)
        tracked = git("ls-files", "-z", cwd=dest, capture_output=True).stdout
        for name in tracked.decode().split("\0"):
            if name and phase_markdown(name):
                git("rm", "--", name, cwd=dest)
        for name in names:
            target = dest / name
            # Do not follow a symlink inherited from the remote checkout.
            for ancestor in (target, *target.parents):
                if ancestor == dest:
                    break
                if ancestor.is_symlink():
                    raise RuntimeError(f"Review destination symlink: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        # Explicit source paths honor our checked manifest even when the old
        # destination had broader ignore rules. No generated files are staged.
        for offset in range(0, len(names), 100):
            git("add", "-f", "--", *names[offset:offset + 100], cwd=dest)
        tracked = git("ls-files", "-z", cwd=dest, capture_output=True).stdout
        if any(phase_markdown(n) for n in tracked.decode().split("\0") if n):
            raise RuntimeError("Phase Markdown found in publication index")
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=dest)
        if diff.returncode == 0:
            print("Destination already matches the selected source files.")
            return
        if diff.returncode != 1:
            raise RuntimeError("Could not inspect staged changes")
        git("diff", "--cached", "--shortstat", cwd=dest)
        print("Creating the publication commit...", flush=True)
        git("commit", "-m", "Update Sandtable application source; exclude phase documents", cwd=dest)
        print(f"Pushing to {REMOTE} ...", flush=True)
        git("push", "origin", "HEAD", cwd=dest)
        git("log", "-1", "--format=Published commit: %H", cwd=dest)


if __name__ == "__main__":
    main()
