"""Phase 17 Batch C acceptance (§17.6–§17.8; finding G): the durable
deliverable contract, staged publication without partial-success
windows, and manifest/download integrity."""
import pytest
import pytest_asyncio

from backend.models.deliverable import (
    SUPPORTED_DELIVERABLES, Deliverable, content_checksum, new_deliverable_id)
from backend.repositories.deliverable_repository import DeliverableRepository
from backend.services.deliverable_publisher import (
    PublicationTransaction, publish_deliverable, reconcile_interrupted,
    validate_manifest)


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "dlv.db"))
    await db.initialize()
    r = DeliverableRepository(db)
    try:
        yield r
    finally:
        await db.close()


def _deliverable(owner="alice", key=None, **over):
    d = Deliverable(
        deliverable_id=new_deliverable_id(), deliverable_key=key or "dlvk-t",
        revision=1, tenant_id="t1", owner_user_id=owner,
        deliverable_type="resume_html",
        file_manifest=[{"path": "resume.html", "content": "<html/>"}],
        lineage={"generator_version": "g1"})
    for k, v in over.items():
        setattr(d, k, v)
    return d


# ── §17.1/§17.6: the supported set is declared, narrow, honest ─────────

def test_supported_deliverables_declare_verification_and_limits():
    for dtype, spec in SUPPORTED_DELIVERABLES.items():
        assert spec["verification"], f"{dtype} declares its checks"
        assert spec["not_yet_verified"], \
            f"{dtype} states what the product does NOT yet verify"
        assert spec["max_bytes"] > 0
    # The unproven runner stays honest: tested projects declare what is
    # still unverified.
    assert "runner" in SUPPORTED_DELIVERABLES["source_project"]["not_yet_verified"][0].lower() or \
        "execution" in SUPPORTED_DELIVERABLES["source_project"]["not_yet_verified"][0].lower()


def test_invalid_deliverables_are_refused():
    with pytest.raises(ValueError, match="integrity metadata"):
        Deliverable(
            deliverable_id="d", deliverable_key="k", revision=1,
            tenant_id="t", owner_user_id="u", deliverable_type="resume_html",
            file_manifest=[{"path": "resume.html"}]   # no sha256/length
        ).validate()
    with pytest.raises(ValueError, match="unsupported deliverable type"):
        Deliverable(
            deliverable_id="d", deliverable_key="k", revision=1,
            tenant_id="t", owner_user_id="u",
            deliverable_type="docx_presentation",
            file_manifest=[{"path": "f", "sha256": "x", "byte_length": 1}],
        ).validate()


# ── §17.6: restart-readable records ────────────────────────────────────

@pytest.mark.asyncio
async def test_deliverable_records_identify_what_from_what(repo):
    d = _deliverable()
    created = await repo.create_candidate(d, deliverable_key="resume-2026")
    stored = await repo.transition(
        created.deliverable_id, "stored",
        file_manifest=[{"path": "resume.html", "sha256": "abc",
                        "byte_length": 8}],
        content_checksum="csum-1",
        verification={"verdict": "pass", "verifier_version": "1.0"})
    got = await repo.get_for_owner(created.deliverable_id, "alice")
    assert got.lifecycle_state == "stored"
    assert got.content_checksum == "csum-1"
    assert got.verification["verdict"] == "pass"
    assert got.lineage["generator_version"] == "g1"
    assert got.revision == 1 and got.parent_revision is None
    # Wrong-user lookup looks identical to missing (§12.28).
    assert await repo.get_for_owner(created.deliverable_id, "mallory") is None


@pytest.mark.asyncio
async def test_revisions_progress_with_parent_lineage(repo):
    first = await repo.create_candidate(_deliverable(key="rk"),
                                        deliverable_key="rk")
    second = await repo.create_candidate(_deliverable(key="rk"),
                                         deliverable_key="rk")
    assert second.revision == 2 and second.parent_revision == 1


# ── §17.7: staged publication — crash windows never fake readiness ─────

@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after", ["validated", "checksummed", "stored",
                                        "stored_verified", "published"])
async def test_injected_failure_leaves_unpublished_candidate(repo, fail_after):
    d = await repo.create_candidate(_deliverable(key="pub-x"),
                                    deliverable_key="pub-x")
    files = [{"path": "resume.html", "content": "<html>final</html>",
              "declared_sha256": ""}]
    tx = await publish_deliverable(
        repo=repo, deliverable_id=d.deliverable_id, files=files,
        entrypoint=None, store_fn=None, verify_stored_fn=None,
        emit_fn=None, fail_after=fail_after)
    assert tx.failures, f"the injected failure at {fail_after} recorded"
    rec = reconcile_interrupted(tx, repo=repo)
    assert rec["action"] == "resume_or_cleanup", \
        f"a crash at {fail_after} is not a completed publication"
    got = await repo.get_for_owner(d.deliverable_id, "alice")
    # The guarantee: no crash window fakes DELIVERY or verification —
    # the state is at most 'stored' (the bytes landed) and never
    # 'delivered'/'verification_passed'.
    assert got.lifecycle_state not in ("delivered", "verification_passed",
                                       "review_completed"), \
        f"a crash at {fail_after} must not fake readiness"
    if fail_after in ("validated", "checksummed"):
        assert got.lifecycle_state == "generated", \
            "before storage, the record stays a candidate"


@pytest.mark.asyncio
async def test_clean_publication_reaches_completion(repo):
    d = await repo.create_candidate(_deliverable(key="pub-ok"),
                                    deliverable_key="pub-ok")
    emitted = []
    async def emit(did, csum):
        emitted.append((did, csum))
    tx = await publish_deliverable(
        repo=repo, deliverable_id=d.deliverable_id,
        files=[{"path": "resume.html", "content": "<html>ok</html>"}],
        emit_fn=emit)
    assert tx.stage == "completion_emitted" and not tx.failures
    assert emitted and emitted[0][0] == d.deliverable_id
    got = await repo.get_for_owner(d.deliverable_id, "alice")
    assert got.content_checksum, "the published bytes ARE the identity"


# ── §17.8: manifest integrity ──────────────────────────────────────────

def test_manifest_integrity_rules():
    with pytest.raises(ValueError, match="missing integrity"):
        validate_manifest([{"path": "a.html"}])
    with pytest.raises(ValueError, match="escaping path"):
        validate_manifest([{"path": "../etc/passwd", "sha256": "x",
                            "byte_length": 1}])
    with pytest.raises(ValueError, match="reserved path"):
        validate_manifest([{"path": "artifact.json", "sha256": "x",
                            "byte_length": 1}])
    with pytest.raises(ValueError, match="entrypoint"):
        validate_manifest([{"path": "main.py", "sha256": "x",
                            "byte_length": 1}], entrypoint="app.py")
    with pytest.raises(ValueError, match="duplicate path"):
        validate_manifest([{"path": "a", "sha256": "x", "byte_length": 1},
                           {"path": "a", "sha256": "y", "byte_length": 2}])
    validate_manifest([{"path": "a.html", "sha256": "x", "byte_length": 1}])


def test_checksums_hash_actual_bytes():
    import hashlib as _hl
    files = [{"path": "f.bin", "content": "binary-ish",
              "sha256": _hl.sha256(b"binary-ish").hexdigest(),
              "byte_length": len(b"binary-ish")}]
    csum = content_checksum(files)
    assert csum and len(csum) == 32
    # Changing a byte changes the checksum (the bytes ARE the contract).
    files[0]["content"] = "binary-ISH"
    files[0]["sha256"] = _hl.sha256(b"binary-ISH").hexdigest()
    assert content_checksum(files) != csum
