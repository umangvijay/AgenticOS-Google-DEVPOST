"""Build preparation uses verified archive bytes and fails closed on bad images."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

from scripts.gcp import prepare_release as release
from scripts.source_identity import fingerprint_from_hashes
from scripts.verify_candidate import reconstruct_candidate


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidates"
    directory = root / "release-test"
    directory.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    hashes = {}
    for name in ("Dockerfile.api", "Dockerfile.worker", "frontend/Dockerfile.frontend"):
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("FROM scratch\n")
        path.chmod(0o644)
        hashes[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": "0o644"}
    sha = fingerprint_from_hashes(hashes)
    archive = directory / "source-archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in hashes:
            tar.add(source / name, arcname=name)
    (directory / "file-hashes.json").write_text(json.dumps(hashes))
    (directory / "manifest.json").write_text(json.dumps({
        "candidate_id": directory.name, "source": {"content_sha256": sha},
        "verification": {"expected_content_sha256": sha},
        "source_archive": {"path": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    args = SimpleNamespace(candidate=directory.name, project="agentos-test", region="us-central1",
        repository="agentos", api_url="https://staging.example.test/api/v1", platform="linux/amd64",
        output=str(tmp_path / "attempt"), build=False)
    return root, directory, sha, args


def test_plan_requires_no_external_commands_and_bakes_correct_frontend_url(candidate, monkeypatch):
    root, _, sha, args = candidate
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("Unexpected cloud/build call"))
    receipt = release.prepare(args, candidates=root)
    assert receipt["status"] == "prepared_only" and receipt["candidate_sha256"] == sha
    assert receipt["images"] == {} and receipt["production_qualified"] is False
    out = Path(args.output)
    assert not (out / "image-values.json").exists()
    commands = json.loads((out / "build-plan.json").read_text())["commands"]
    assert commands["frontend"]["context"] == "frontend"
    assert commands["api"]["context"] == commands["worker"]["context"] == "."
    assert "NEXT_PUBLIC_API_URL=" + args.api_url in commands["frontend"]["argv"]


def simulated_builder(candidate, monkeypatch, fault=None):
    root, _, sha, args = candidate
    args.build = True
    calls = []
    manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "1"*64}, "layers": []}
    if fault == "index":
        manifest = {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []}
    raw = json.dumps(manifest).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    def run(argv, *, cwd, log):
        calls.append((argv, cwd))
        if argv[0] == "gcloud":
            if fault == "repository":
                raise OSError("No access")
            return
        assert (cwd / argv[argv.index("--file")+1]).read_text() == "FROM scratch\n"
        if fault == "source":
            (cwd / "Dockerfile.api").write_text("CHANGED\n")
        if fault == "partial" and "worker-build.log" == log.name:
            raise OSError("Worker build stopped")
        meta = Path(argv[argv.index("--metadata-file")+1])
        meta.write_text(json.dumps({"containerimage.digest": "bad" if fault == "digest" else digest}))
    def registry(argv, *, cwd, log):
        assert argv[-1].endswith("@" + digest)
        if "--raw" in argv:
            return b'{}' if fault == "registry" else raw + b"\n"
        return json.dumps({"os":"linux", "architecture":"arm64" if fault == "platform" else "amd64",
            "config":{"Labels":{"io.agentos.source-sha256":"wrong" if fault == "label" else sha}}}).encode()
    monkeypatch.setattr(release, "run", run)
    monkeypatch.setattr(release, "read_registry", registry)
    return root, args, digest, calls


def test_built_images_are_read_back_and_all_helm_roles_pinned(candidate, monkeypatch):
    root, args, digest, calls = simulated_builder(candidate, monkeypatch)
    receipt = release.prepare(args, candidates=root)
    assert receipt["status"] == "images_published"
    assert receipt["deployment_verified"] is receipt["production_qualified"] is False
    assert len(calls) == 4
    assert set(receipt["images"]) == {"api", "worker", "frontend"}
    values = json.loads((Path(args.output) / "image-values.json").read_text())
    for role in ("api", "worker", "frontend", "browserWorker", "runner"):
        assert values[role]["imageDigest"] == digest
    assert values["runner"]["image"] == values["browserWorker"]["image"] == "worker"
    assert all(image["reference"].endswith("@"+digest) for image in receipt["images"].values())


@pytest.mark.parametrize("fault", ["repository", "source", "partial", "digest", "registry", "index", "platform", "label"])
def test_failed_build_never_publishes_release_values(candidate, monkeypatch, fault):
    root, args, _, _ = simulated_builder(candidate, monkeypatch, fault)
    with pytest.raises((ValueError, OSError)):
        release.prepare(args, candidates=root)
    output = Path(args.output)
    receipt = json.loads((output / "build-receipt.json").read_text())
    assert receipt["status"] == "failed" and receipt["production_qualified"] is False
    assert not (output / "image-values.json").exists()
    if fault == "partial":
        assert set(receipt["images"]) == {"api"}


@pytest.mark.parametrize("field,value", [("candidate", "../escape"), ("project", "--other-project"),
    ("region", "us-central1;id"), ("repository", "a/b"), ("api_url", "http://staging.test/api/v1"),
    ("api_url", "https://user:secret@staging.test/api/v1"), ("api_url", "https://staging.test"),
    ("api_url", "https://staging.test/api/v1?token=secret")])
def test_invalid_configuration_refused_before_output(candidate, field, value):
    root, _, _, args = candidate
    setattr(args, field, value)
    with pytest.raises(ValueError):
        release.prepare(args, candidates=root)
    assert not Path(args.output).exists()


def test_earlier_attempt_is_never_overwritten(candidate):
    root, _, _, args = candidate
    release.prepare(args, candidates=root)
    receipt = Path(args.output) / "build-receipt.json"
    original = receipt.read_bytes()
    with pytest.raises(FileExistsError):
        release.prepare(args, candidates=root)
    assert receipt.read_bytes() == original


@pytest.mark.parametrize("fault", ["bytes", "source_identity", "archive_path", "candidate_identity"])
def test_unverified_candidate_never_reaches_builder(candidate, monkeypatch, fault):
    root, directory, _, args = candidate
    args.build = True
    monkeypatch.setattr(release, "run", lambda *a, **kw: pytest.fail("Unverified source reached builder"))
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    if fault == "bytes":
        (directory / "source-archive.tar.gz").write_bytes(b"corrupt")
    elif fault == "source_identity":
        manifest["source"]["content_sha256"] = "f"*64
    elif fault == "archive_path":
        manifest["source_archive"]["path"] = "../outside.tar.gz"
    else:
        manifest["candidate_id"] = "other"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        release.prepare(args, candidates=root)
    assert json.loads((Path(args.output) / "build-receipt.json").read_text())["status"] == "failed"


def test_reconstruction_refuses_existing_files(candidate, tmp_path):
    _, directory, _, _ = candidate
    target = tmp_path / "existing"
    target.mkdir()
    (target / "secret").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        reconstruct_candidate(directory, target)
    assert (target / "secret").read_text() == "keep"
