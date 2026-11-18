"""Versioned screenshot/preview descriptor (§29.12/§29.30–29.33 revision).

A preview is EVIDENCE of one observation — a distinct resource from the
live browser session, the artifact revision, and any UI tab. The
descriptor persists:

- tenant ownership and an immutable resource reference + revision;
- the authoritative normalized ORIGIN (explicit port preserved) separate
  from the sanitized display URL;
- fetch provenance (method, attempt, captured-at);
- the image digest and an owner-authorized blob reference — cached bytes
  become inaccessible the moment the descriptor is revoked;
- an explicit expiry.

Interactive control is NOT the preview: `open_interactive_session` is an
explicit, owner-authorized transition that returns a NEW session ID,
recorded on the descriptor but distinct from `preview_id`,
`artifact_revision`, and any UI `tab_id`.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

SCHEMA_VERSION = 2
DEFAULT_TTL_SECONDS = 15 * 60


class PreviewAccessError(Exception):
    """An authorized access boundary refused the request."""


@dataclass
class PreviewDescriptor:
    schema_version: int = SCHEMA_VERSION
    preview_id: str = ""
    tenant_id: str = ""
    resource: Dict[str, Any] = field(default_factory=dict)
    artifact_revision: Optional[str] = None
    status: str = "ready"                     # ready | expired | revoked
    display_url: str = ""                     # sanitized (no query/fragment)
    source_origin: str = ""                   # authoritative scheme://host:port
    resolved_destination_ref: Optional[str] = None
    fetch: Dict[str, Any] = field(default_factory=dict)
    image: Dict[str, Any] = field(default_factory=dict)  # blob_ref, sha256, mime
    expires_at: int = 0                       # epoch seconds
    browser_session_id: Optional[str] = None  # set only by an explicit open
    interactive_sessions: Dict[str, Any] = field(default_factory=dict)
    denial: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PreviewStore:
    """Process-local reference store. The durable home is the artifact
    store / browser-session repos; this contract pins the descriptor
    semantics the panels consume."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
                 clock=None):
        self._ttl = int(ttl_seconds)
        self._clock = clock or (lambda: int(time.time()))
        self._descriptors: Dict[str, PreviewDescriptor] = {}
        self._blobs: Dict[str, bytes] = {}

    # ── creation ───────────────────────────────────────────────────────

    def new_preview_id(self) -> str:
        return f"pv_{secrets.token_hex(8)}"

    def build(self, *, tenant_id: str, resource_kind: str, resource_id: str,
              resource_revision: str, origin: str, display_url: str,
              image_bytes: bytes, mime: str = "image/jpeg",
              captured_by_attempt: Optional[str] = None,
              capture_method: str = "owner_worker_capture",
              artifact_revision: Optional[str] = None,
              resolved_destination_ref: Optional[str] = None,
              ttl_seconds: Optional[int] = None) -> PreviewDescriptor:
        if not origin or "://" not in origin:
            raise PreviewAccessError(
                "a preview requires the authoritative normalized origin")
        now = self._clock()
        blob_ref = f"private_blob_{secrets.token_hex(8)}"
        self._blobs[blob_ref] = image_bytes
        descriptor = PreviewDescriptor(
            preview_id=self.new_preview_id(),
            tenant_id=tenant_id,
            resource={"kind": resource_kind, "id": resource_id,
                      "revision": resource_revision},
            artifact_revision=artifact_revision,
            status="ready",
            display_url=display_url,
            source_origin=origin,
            resolved_destination_ref=resolved_destination_ref,
            fetch={"method": capture_method,
                   "attempt_id": captured_by_attempt,
                   "captured_at": now},
            image={"blob_ref": blob_ref,
                   "sha256": hashlib.sha256(image_bytes).hexdigest(),
                   "mime": mime, "bytes": len(image_bytes)},
            expires_at=now + int(ttl_seconds or self._ttl),
        )
        self._descriptors[descriptor.preview_id] = descriptor
        return descriptor

    # ── authorized access ──────────────────────────────────────────────

    def _authorized(self, tenant_id: str, preview_id: str) -> \
            PreviewDescriptor:
        descriptor = self._descriptors.get(preview_id)
        if descriptor is None or descriptor.tenant_id != tenant_id:
            # Foreign and unknown previews look identical (no existence leak).
            raise PreviewAccessError("preview not found for this tenant")
        if descriptor.status == "revoked":
            raise PreviewAccessError("preview revoked")
        if descriptor.expires_at and self._clock() >= descriptor.expires_at:
            descriptor.status = "expired"
            descriptor.denial = "preview expired; a refresh may re-capture"
            raise PreviewAccessError(descriptor.denial)
        return descriptor

    def authorize(self, *, tenant_id: str, preview_id: str) -> Dict[str, Any]:
        """Authorized read: descriptor WITHOUT raw bytes."""
        return self._authorized(tenant_id, preview_id).to_dict()

    def read_image(self, *, tenant_id: str, preview_id: str) -> \
            tuple[bytes, Dict[str, Any]]:
        """Authorized byte read through the blob reference. Revocation or
        expiry invalidates the cached bytes."""
        descriptor = self._authorized(tenant_id, preview_id)
        blob = self._blobs.get(descriptor.image.get("blob_ref") or "")
        if blob is None:
            raise PreviewAccessError("preview bytes unavailable")
        return blob, descriptor.image

    def revoke(self, *, tenant_id: str, preview_id: str) -> bool:
        """Revocation invalidates access to the cached bytes and leaves
        the denial explanation readable by the owner."""
        descriptor = self._descriptors.get(preview_id)
        if descriptor is None or descriptor.tenant_id != tenant_id:
            return False
        descriptor.status = "revoked"
        descriptor.denial = "preview revoked by the owner"
        self._blobs.pop(descriptor.image.get("blob_ref") or "", None)
        return True

    # ── explicit open-to-session ───────────────────────────────────────

    def open_interactive_session(self, *, tenant_id: str, preview_id: str,
                                 actor_user_id: str) -> str:
        """The EXPLICIT interactive transition: authorizes the destination
        and returns a NEW session id — distinct from the preview id, the
        artifact revision, and any UI tab id — recorded on the
        descriptor with its controller generation (0 = fresh)."""
        descriptor = self._authorized(tenant_id, preview_id)
        session_id = f"pvsession_{secrets.token_hex(8)}"
        descriptor.browser_session_id = session_id
        descriptor.interactive_sessions[session_id] = {
            "actor_user_id": actor_user_id,
            "opened_at": self._clock(),
            "controller_generation": 0,
        }
        return session_id


def descriptor_from_capture(capture: Dict[str, Any], *, tenant_id: str,
                            store: PreviewStore, resource_id: str,
                            resource_revision: str,
                            captured_by_attempt: Optional[str] = None,
                            artifact_revision: Optional[str] = None,
                            ) -> PreviewDescriptor:
    """Build a descriptor from WebAgent's `_capture_preview` output —
    the capture's `origin` (authoritative, port-preserved) and its
    masked image bytes flow straight through."""
    import base64
    image_field = capture.get("image") or ""
    if image_field.startswith("data:image/jpeg;base64,"):
        image_bytes = base64.b64decode(image_field.split(",", 1)[1])
        mime = "image/jpeg"
    else:
        raise PreviewAccessError("capture is not a masked JPEG preview")
    return store.build(
        tenant_id=tenant_id,
        resource_kind="browser_observation",
        resource_id=resource_id,
        resource_revision=resource_revision,
        origin=capture.get("origin") or "",
        display_url=capture.get("url") or "",
        image_bytes=image_bytes,
        mime=mime,
        captured_by_attempt=captured_by_attempt,
        artifact_revision=artifact_revision,
    )
