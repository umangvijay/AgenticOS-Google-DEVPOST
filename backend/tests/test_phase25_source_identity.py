"""Phase 25 §25.1 — ONE fingerprint implementation everywhere.

Regressions: a source edit changes the fingerprint; writing an evidence
report does NOT; the freezer, verifier, and qualifier produce identical
fingerprints for the same tree.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from source_identity import source_fingerprint, fingerprint_from_hashes  # noqa: E402


def test_source_edit_changes_fingerprint(tmp_path):
    """The fingerprint must be sensitive to content."""
    root = tmp_path
    (root / "mod.py").write_text("x = 1\n")
    fp1, _ = source_fingerprint(root)
    (root / "mod.py").write_text("x = 2\n")
    fp2, _ = source_fingerprint(root)
    assert fp1 != fp2, "an untouched fingerprint would blind the release"


def test_mode_change_changes_fingerprint(tmp_path):
    root = tmp_path
    f = root / "run.sh"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    fp1, _ = source_fingerprint(root)
    f.chmod(0o755)
    fp2, _ = source_fingerprint(root)
    assert fp1 != fp2, "mode drift is part of the identity (audit finding)"


def test_evidence_write_does_not_change_fingerprint():
    """Writing evidence under docs/evidence/ must not alter identity —
    otherwise every attempt invalidates itself (excluded by rule)."""
    before, _ = source_fingerprint(ROOT)
    probe = ROOT / "docs/evidence/fingerprint-probe.json"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(json.dumps({"probe": "identity-neutral"}))
    try:
        after, _ = source_fingerprint(ROOT)
        assert after == before, \
            "evidence writes must not change the source identity"
    finally:
        probe.unlink(missing_ok=True)


def test_freeze_and_qualifier_agree():
    """The freezer's fingerprint function and the qualifier's identity
    must produce the SAME digest for the same tree."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "qual", ROOT / "scripts/qualify_release.py")
    qual = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qual)
    fp_freeze, _ = source_fingerprint(ROOT)
    fp_qual = qual.git_identity()["content_sha256"]
    assert fp_freeze == fp_qual, \
        "divergent identity calculations are the Phase 24 audit finding"


def test_fingerprint_algorithm_is_stable(tmp_path):
    """A frozen golden vector: the canonical algorithm must not drift."""
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "b.txt").write_bytes(b"beta")
    fp, _ = source_fingerprint(tmp_path)
    # recompute by hand with the documented algorithm
    d = hashlib.sha256()
    for name, content in (("a.txt", b"alpha"), ("b.txt", b"beta")):
        d.update(name.encode() + b"\0")
        d.update(hashlib.sha256(content).digest())
        d.update(oct(0o644).encode())
        d.update(b"\0")
    assert fp == d.hexdigest()


def test_secret_variants_excluded_and_symlinks_refused(tmp_path):
    for name in (".env.production", ".env.staging.local", ".env.test"):
        (tmp_path / name).write_text("secret")
    (tmp_path / ".env.example").write_text("API_KEY=")
    _, files = source_fingerprint(tmp_path)
    assert set(files) == {".env.example"}
    (tmp_path / "leak.txt").symlink_to(tmp_path / ".env.production")
    with pytest.raises(ValueError, match="symlinks"):
        source_fingerprint(tmp_path)


@pytest.mark.parametrize("extra", ["../escape.py", "/tmp/escape.py", "symlink", "extra.py", "duplicate"])
def test_verifier_refuses_unsafe_or_unlisted_members(tmp_path, monkeypatch, extra):
    import io
    import tarfile
    from scripts import verify_candidate
    folder = tmp_path / "candidate"
    folder.mkdir()
    archive = folder / "source-archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("main.py")
        member.size = 1
        member.mode = 0o644
        tar.addfile(member, io.BytesIO(b"x"))
        member = tarfile.TarInfo("main.py" if extra == "duplicate" else extra)
        if extra == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "../escape.py"
        else:
            member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    hashes = {"main.py": {"sha256": hashlib.sha256(b"x").hexdigest(), "mode": "0o644"}}
    (folder / "file-hashes.json").write_text(json.dumps(hashes))
    (folder / "manifest.json").write_text(json.dumps({
        "source_archive": {"path": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        "verification": {"expected_content_sha256": fingerprint_from_hashes(hashes)}}))
    monkeypatch.setattr(verify_candidate, "CANDIDATES", tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify_candidate.py", "candidate"])
    assert verify_candidate.main() == 1
    assert not (tmp_path / "escape.py").exists()
