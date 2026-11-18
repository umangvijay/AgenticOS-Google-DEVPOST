"""Phase 29 revision — the screenshot/preview descriptor contract
(§29.12/§29.30–29.33).

Preview, artifact revision, tab and live browser session IDs are
DISTINCT; the authoritative origin preserves the explicit port; foreign
tenants see "not found"; revocation invalidates the cached bytes; an
expired preview keeps its explanation; open-to-session is an explicit
authorized transition returning a NEW session id.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from backend.services.preview_descriptor import (
    PreviewAccessError, PreviewStore, descriptor_from_capture,
)


@pytest.fixture()
def store():
    return PreviewStore(ttl_seconds=900, clock=lambda: 1_000_000)


def _capture():
    """A WebAgent `_capture_preview`-shaped output: the display URL
    preserves the explicit port and strips the query; `origin` is the
    authoritative scheme://host:port (no path, no query)."""
    image = b"\xff\xd8fakejpeg"
    return {"image": "data:image/jpeg;base64," +
            base64.b64encode(image).decode("ascii"),
            "url": "https://changes.example.test:8443/app",
            "origin": "https://changes.example.test:8443", "step": 2}


def test_descriptor_keeps_ids_and_origin_distinct(store):
    descriptor = descriptor_from_capture(
        _capture(), tenant_id="t1", store=store,
        resource_id="obs_7", resource_revision="sha256:" + "a" * 64,
        captured_by_attempt="a2", artifact_revision="art-rev-1")
    assert descriptor.schema_version == 2
    assert descriptor.preview_id != descriptor.browser_session_id, \
        "the session id only exists after an explicit open"
    assert descriptor.artifact_revision == "art-rev-1"
    assert descriptor.preview_id != descriptor.artifact_revision
    assert descriptor.source_origin == "https://changes.example.test:8443"
    assert descriptor.fetch["attempt_id"] == "a2"
    assert descriptor.fetch["method"] == "owner_worker_capture"


def test_owner_reads_bytes_and_foreign_tenants_cannot(store):
    descriptor = descriptor_from_capture(
        _capture(), tenant_id="t1", store=store,
        resource_id="obs_7", resource_revision="sha256:" + "a" * 64)
    blob, image = store.read_image(tenant_id="t1",
                                   preview_id=descriptor.preview_id)
    assert hashlib.sha256(blob).hexdigest() == image["sha256"]
    with pytest.raises(PreviewAccessError, match="not found"):
        store.authorize(tenant_id="t2", preview_id=descriptor.preview_id)
    with pytest.raises(PreviewAccessError, match="not found"):
        store.read_image(tenant_id="t2", preview_id=descriptor.preview_id)


def test_revocation_invalidates_cached_bytes_and_keeps_explanation(store):
    descriptor = descriptor_from_capture(
        _capture(), tenant_id="t1", store=store,
        resource_id="obs_7", resource_revision="sha256:" + "a" * 64)
    assert store.revoke(tenant_id="t1", preview_id=descriptor.preview_id)
    with pytest.raises(PreviewAccessError, match="revoked"):
        store.read_image(tenant_id="t1", preview_id=descriptor.preview_id)
    # The owner's descriptor record retains the explanation.
    record = store.authorize.__self__._descriptors[descriptor.preview_id]
    assert record.status == "revoked"
    assert record.denial == "preview revoked by the owner"
    assert record.image.get("blob_ref") not in store._blobs


def test_expired_preview_denies_with_explanation(store):
    descriptor = descriptor_from_capture(
        _capture(), tenant_id="t1", store=store,
        resource_id="obs_7", resource_revision="sha256:" + "a" * 64)
    store._clock = lambda: descriptor.expires_at + 1
    with pytest.raises(PreviewAccessError, match="expired"):
        store.authorize(tenant_id="t1", preview_id=descriptor.preview_id)
    record = store._descriptors[descriptor.preview_id]
    assert record.status == "expired"
    assert "refresh" in (record.denial or "")


def test_open_to_session_is_explicit_and_distinct(store):
    descriptor = descriptor_from_capture(
        _capture(), tenant_id="t1", store=store,
        resource_id="obs_7", resource_revision="sha256:" + "a" * 64)
    before = descriptor.browser_session_id
    session_id = store.open_interactive_session(
        tenant_id="t1", preview_id=descriptor.preview_id,
        actor_user_id="owner-1")
    assert session_id.startswith("pvsession_")
    assert before is None and descriptor.browser_session_id == session_id
    assert descriptor.preview_id != session_id
    session = descriptor.interactive_sessions[session_id]
    assert session["actor_user_id"] == "owner-1"
    assert session["controller_generation"] == 0
    # A foreign actor cannot open.
    with pytest.raises(PreviewAccessError):
        store.open_interactive_session(
            tenant_id="t2", preview_id=descriptor.preview_id,
            actor_user_id="stranger")


def test_origin_without_authority_is_refused(store):
    with pytest.raises(PreviewAccessError, match="origin"):
        store.build(tenant_id="t1", resource_kind="browser_observation",
                    resource_id="o", resource_revision="sha256:" + "b" * 64,
                    origin="changes.example.test:8443",
                    display_url="https://changes.example.test:8443/",
                    image_bytes=b"\xff\xd8x")
