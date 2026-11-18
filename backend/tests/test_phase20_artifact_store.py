"""Phase 20 §20.6: artifact store contract — immutable revision
identities, per-file digests verified on read, authorized download
(server-enforced ownership + grants), owner-only delete, and blob
immutability. Every test drives the REAL store over the REAL
filesystem (tmp_path); no mocks."""
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

from backend.services.artifact_store import (
    ArtifactNotFound,
    ArtifactNotAuthorized,
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


async def _put(store, owner="owner-1", run="run-1", name="report.txt",
               content=b"hello artifact", ctype="text/plain"):
    return await store.put(owner, run, name, content, ctype)


# ── write + identity ───────────────────────────────────────────────────

async def test_put_returns_digest_verified_identity(store):
    meta, content = await store.get("owner-1",
                                    (await _put(store)).artifact_id)
    assert content == b"hello artifact"
    assert meta.byte_length == len(content)
    assert meta.revision == 1 and meta.parent_artifact_id is None
    assert meta.content_type == "text/plain"


async def test_identical_bytes_return_the_same_identity(store):
    """Immutable identity: re-putting the SAME bytes is idempotent — the
    first identity is returned, never duplicated."""
    first = await _put(store)
    second = await _put(store, content=b"hello artifact")
    assert second.artifact_id == first.artifact_id
    assert second.revision == first.revision == 1


async def test_changed_bytes_chain_a_new_revision(store):
    first = await _put(store, content=b"v1")
    second = await _put(store, content=b"v2")
    assert second.artifact_id != first.artifact_id
    assert second.revision == 2
    assert second.parent_artifact_id == first.artifact_id
    # BOTH revisions remain readable (history is preserved).
    m1, c1 = await store.get("owner-1", first.artifact_id)
    m2, c2 = await store.get("owner-1", second.artifact_id)
    assert (c1, c2) == (b"v1", b"v2")


# ── integrity ──────────────────────────────────────────────────────────

async def test_read_verifies_digest_of_stored_bytes(store, tmp_path):
    meta = await _put(store, content=b"integrity check bytes")
    # Corrupt the stored blob out-of-band.
    blob = next((tmp_path / "artifacts" / "blobs").rglob(meta.content_sha256))
    blob.write_bytes(b"tampered bytes here!")
    with pytest.raises(ArtifactIntegrityError, match="do not match"):
        await store.get("owner-1", meta.artifact_id)


async def test_missing_blob_is_an_integrity_failure(store, tmp_path):
    meta = await _put(store)
    blob = next((tmp_path / "artifacts" / "blobs").rglob(meta.content_sha256))
    blob.unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        await store.get("owner-1", meta.artifact_id)


async def test_blobs_are_write_once(store, tmp_path):
    """Two artifacts with identical bytes share one blob; the store
    never rewrites an existing blob (O_EXCL contract)."""
    a = await _put(store, run="run-a", content=b"shared bytes")
    b = await _put(store, run="run-b", content=b"shared bytes")
    blobs = list((tmp_path / "artifacts" / "blobs").rglob("*"))
    blob_files = [p for p in blobs if p.is_file()]
    assert len(blob_files) == 1, "identical bytes dedupe to one blob"
    assert a.artifact_id != b.artifact_id


# ── authorized download ────────────────────────────────────────────────

async def test_foreign_actor_cannot_read_or_discover(store):
    meta = await _put(store)
    # A stranger gets the SAME error as a missing artifact — existence
    # is never leaked to non-owners.
    stranger_err = None
    missing_err = None
    try:
        await store.get("stranger-9", meta.artifact_id)
    except ArtifactStoreError as e:
        stranger_err = str(e)
    try:
        await store.get("stranger-9", "a-does-not-exist-000000")
    except ArtifactStoreError as e:
        missing_err = str(e)
    assert ("does not exist" in stranger_err
            and "does not exist" in missing_err), (
        "non-owners learn nothing beyond 'does not exist' — both errors "
        "are indistinguishable (existence is never leaked)")


async def test_grant_revoke_controls_download(store):
    meta = await _put(store)
    with pytest.raises(ArtifactNotFound):
        await store.get("colleague-2", meta.artifact_id)
    await store.grant("owner-1", meta.artifact_id, "colleague-2")
    m, content = await store.get("colleague-2", meta.artifact_id)
    assert content == b"hello artifact", "granted actor may download"
    # Non-owner cannot grant themselves.
    with pytest.raises(ArtifactNotAuthorized):
        await store.grant("colleague-2", meta.artifact_id, "colleague-2")
    await store.revoke("owner-1", meta.artifact_id, "colleague-2")
    with pytest.raises(ArtifactNotFound):
        await store.get("colleague-2", meta.artifact_id)


async def test_metadata_and_listing_are_scoped(store):
    await _put(store, run="run-1", name="a.txt")
    await _put(store, run="run-1", name="b.txt")
    await _put(store, run="run-2", name="c.txt")
    items = await store.list("owner-1", "run-1")
    assert [m.name for m in items] == ["a.txt", "b.txt"]
    stranger_items = await store.list("stranger-9", "run-1")
    assert stranger_items == []


# ── delete ─────────────────────────────────────────────────────────────

async def test_delete_is_owner_only(store):
    meta = await _put(store)
    with pytest.raises(ArtifactNotAuthorized):
        await store.delete("stranger-9", meta.artifact_id)
    assert await store.delete("owner-1", meta.artifact_id)
    with pytest.raises(ArtifactNotFound):
        await store.get("owner-1", meta.artifact_id)
    with pytest.raises(ArtifactNotFound):
        await store.delete("owner-1", meta.artifact_id)


async def test_delete_refuses_when_revisions_descend(store):
    """A parent revision cannot be deleted while a later revision
    descends from it — deleting the middle of a chain would silently
    rewrite history."""
    first = await _put(store, content=b"v1")
    second = await _put(store, content=b"v2")
    with pytest.raises(ArtifactStoreError, match="descend"):
        await store.delete("owner-1", first.artifact_id)
    # Tail-first deletion works.
    assert await store.delete("owner-1", second.artifact_id)
    assert await store.delete("owner-1", first.artifact_id)
