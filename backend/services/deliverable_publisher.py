"""Staged deliverable publication (Phase 17 §17.7) — no partial-success
windows. Object-store writes and DB writes are NOT one transaction, so
each stage is durable and a crash anywhere leaves an UNPUBLISHED
candidate. Interrupted publications reconcile explicitly."""
from __future__ import annotations

import hashlib
import logging

from backend.models.admission import epoch_now
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("deliverable_publisher")

PUBLICATION_STAGES = ("generation_allocated", "staged", "validated",
                      "checksummed", "stored", "stored_verified",
                      "published", "completion_emitted")


@dataclass
class PublicationTransaction:
    deliverable_id: str
    stage: str = "generation_allocated"
    failures: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def published(self) -> bool:
        return self.stage in ("published", "completion_emitted")


def checksum_file_bytes(content: bytes) -> Dict[str, Any]:
    """§17.8: hash the ACTUAL bytes — sha256 + byte length per file."""
    return {"sha256": hashlib.sha256(content).hexdigest(),
            "byte_length": len(content)}


def validate_manifest(files: List[Dict[str, Any]], *,
                      entrypoint: Optional[str] = None,
                      require_integrity: bool = True) -> None:
    """§17.8: a checksum + byte length for EVERY file; an entrypoint that
    exists; no duplicate/reserved/escaping paths. `require_integrity=False`
    is the PRE-checksum structural pass."""
    seen = set()
    problems = []
    for f in files or []:
        path = str(f.get("path") or "")
        if not path:
            problems.append("a file entry lacks a path")
            continue
        if path in seen:
            problems.append(f"duplicate path: {path}")
        seen.add(path)
        norm = path.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/"):
            problems.append(f"escaping path: {path}")
        if path in ("artifact.json",) or norm.startswith(".git/"):
            problems.append(f"reserved path: {path}")
        if require_integrity and (
                not f.get("sha256") or f.get("byte_length") in (None, 0)):
            problems.append(f"missing integrity metadata: {path}")
    if entrypoint and entrypoint not in seen:
        problems.append(f"entrypoint '{entrypoint}' not in the manifest")
    if not files:
        problems.append("empty manifest")
    if problems:
        raise ValueError("manifest invalid: " + "; ".join(problems))


async def publish_deliverable(*, repo, deliverable_id: str,
                              files: List[Dict[str, Any]],
                              entrypoint: Optional[str] = None,
                              store_fn: Optional[Callable] = None,
                              verify_stored_fn: Optional[Callable] = None,
                              emit_fn: Optional[Callable] = None,
                              fail_after: Optional[str] = None,
                              require_production_deps: bool = False,
                              ) -> PublicationTransaction:
    """The staged sequence (§17.7): allocate (done at candidate creation)
    → validate → checksum from final bytes → store immutable → verify
    stored → publish manifest → emit completion. `fail_after` injects a
    crash after a named stage — the acceptance harness proves no
    incomplete artifact is ever represented as ready.

    §18 C7: in production (`require_production_deps=True`) the storage,
    verification, and emission dependencies are MANDATORY — a missing
    callback refuses the publication instead of silently advancing the
    transaction as if the bytes were stored."""
    from backend.models.deliverable import content_checksum
    if require_production_deps:
        missing = [name for name, fn in
                   (("store_fn", store_fn), ("verify_stored_fn", verify_stored_fn),
                    ("emit_fn", emit_fn)) if fn is None]
        if missing:
            raise ValueError(
                "production publication requires: " + ", ".join(missing) +
                " — optional callbacks must not simulate successful "
                "publication (§18 C7)")
    tx = PublicationTransaction(deliverable_id=deliverable_id)
    try:
        # Stage 3a: structural validation (paths, duplicates, entrypoint).
        validate_manifest(files, entrypoint=entrypoint,
                          require_integrity=False)
        # Stage 4: checksums from the FINAL bytes.
        for f in files:
            content = (f.get("content") or "").encode("utf-8")
            f.update(checksum_file_bytes(content))
        tx.stage = "checksummed"
        if fail_after == "checksummed":
            raise RuntimeError("injected failure after checksum")
        # Stage 3b: FULL validation now that integrity metadata exists.
        validate_manifest(files, entrypoint=entrypoint)
        tx.stage = "validated"
        if fail_after == "validated":
            raise RuntimeError("injected failure after validation")
        # Stage 5: store immutable content (injected store fn = the
        # object-store boundary; its failure leaves the candidate).
        if store_fn is not None:
            await store_fn(files)
        tx.stage = "stored"
        if fail_after == "stored":
            raise RuntimeError("injected failure after storage")
        # Stage 6: verify the STORED content.
        if verify_stored_fn is not None:
            ok = await verify_stored_fn(files)
            if not ok:
                raise RuntimeError("stored-content verification failed")
        tx.stage = "stored_verified"
        if fail_after == "stored_verified":
            raise RuntimeError("injected failure after stored-verify")
        # Stage 7: publish the authoritative manifest.
        checksum = content_checksum(files)
        await repo.transition(deliverable_id, "stored",
                              file_manifest=files,
                              content_checksum=checksum)
        tx.stage = "published"
        if fail_after == "published":
            raise RuntimeError("injected failure after publish")
        # Stage 8: completion through the durable event mechanism.
        # §19 C3: completion_emitted is claimed ONLY when an emission
        # actually ran — without an emit callback the sequence stops at
        # "published" (the reconciler's completion event covers it).
        if emit_fn is None:
            return tx
        await emit_fn(deliverable_id, checksum)
        tx.stage = "completion_emitted"
        return tx
    except Exception as exc:
        tx.failures.append({"stage": tx.stage, "error": str(exc)[:300]})
        logger.warning("Publication interrupted at %s for %s: %s",
                       tx.stage, deliverable_id, exc)
        return tx


def reconcile_interrupted(tx: PublicationTransaction, *, repo) -> Dict[str, Any]:
    """§17.7: an interrupted publication reconciles EXPLICITLY — the
    candidate is resumable (retry the stages) or cleanable; it is never
    downloadable as 'completed'. Only the FULL sequence (completion
    emitted) counts as done."""
    if tx.stage == "completion_emitted":
        return {"action": "none", "reason": "publication completed"}
    return {"action": "resume_or_cleanup",
            "interrupted_at": tx.stage,
            "deliverable_id": tx.deliverable_id,
            "failures": tx.failures,
            "note": ("a crash before publication leaves an unpublished "
                     "candidate — never a downloadable completed artifact")}


# ── §17.24: source-change and revocation propagation ──────────────────

async def propagate_source_change(*, memory_repo, deliverable_repo,
                                  source_memory_id: str,
                                  change: str) -> Dict[str, Any]:
    """§17.24: a source correction/deletion/revocation propagates through
    Phase 16's dependency graph — affected deliverable revisions are
    identified, their verification becomes STALE (never rewritten), and
    regeneration is offered. The historical revision keeps its audit
    meaning; published bytes are never silently changed."""
    if change not in ("corrected", "deleted", "revoked"):
        raise ValueError(f"unknown source change '{change}'")
    dependents = []
    if hasattr(memory_repo, "dependents_of"):
        dependents = await memory_repo.dependents_of(source_memory_id)
    affected = []
    for dep in dependents:
        # The durable edge rows carry derived_kind; the in-memory map
        # entries carry kind — accept both (§16.21 compat).
        kind = dep.get("derived_kind") or dep.get("kind")
        if kind not in ("delivered_artifact", "verification"):
            continue
        d = await deliverable_repo.get(dep.get("derived_id"))
        if d is None:
            # The edge may point at a record this repo cannot resolve —
            # surface it rather than silently dropping it.
            affected.append({"deliverable_id": dep.get("derived_id"),
                             "verification_stale": None,
                             "reason": "record unresolvable"})
            continue
        verification = dict(d.verification or {})
        verification["stale"] = True
        verification["stale_reason"] = f"source {change}"
        verification["stale_since"] = epoch_now()
        await deliverable_repo.transition(d.deliverable_id,
                                           d.lifecycle_state,
                                           verification=verification)
        affected.append({"deliverable_id": d.deliverable_id,
                         "revision": d.revision,
                         "verification_stale": True})
    return {"source_memory_id": source_memory_id, "change": change,
            "affected": affected,
            "definitions": {
                "source_deleted": "the memory is gone; the deliverable's "
                                  "provenance cites a missing source",
                "deliverable_deleted": "the record + access are removed",
                "verification_invalidated": "the verdict is stale; the "
                                            "bytes and history remain",
            },
            "note": ("correcting a remembered employer marks the affected "
                     "résumé's provenance STALE without rewriting its "
                     "historical revision (§17.24)")}
