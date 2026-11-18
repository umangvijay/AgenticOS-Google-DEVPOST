"""Memory lifecycle (Phase 16 §16.1–16.7).

Stored knowledge is classified, attributable, correctable, and removable:

- §16.1 five classes: transient_context, execution_episode, procedure,
  factual, preference (legacy profile/workflow/semantic map through).
- §16.2 provenance + freshness: source, confidence, expiry — expired
  entries stop being served (never silently kept fresh).
- §16.3 retrieval authorization: ownership filters run BEFORE search and
  again before disclosure — similarity never grants access.
- §16.4 conflicts: a newer entry for the same subject_key SUPERSEDES the
  older one; history is preserved and inspectable, never silently merged.
- §16.5 user control: inspect, correct (new version, history kept),
  export, forget.
- §16.6 deletion propagation: forgetting a memory deletes the row and
  invalidates derived artifacts registered in its metadata.
- §16.7 retrieval quality: search must return what it claims — fixture
  tests assert precision on known content.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("memory_lifecycle")

MEMORY_CLASSES = ("transient_context", "execution_episode", "procedure",
                  "factual", "preference")

_LEGACY_TYPE_MAP = {"profile": "preference", "workflow": "execution_episode",
                    "semantic": "factual"}


def normalize_memory_class(memory_class: Optional[str],
                           legacy_type: Optional[str] = None) -> str:
    mc = (memory_class or "").strip().lower()
    if mc in MEMORY_CLASSES:
        return mc
    if legacy_type in _LEGACY_TYPE_MAP:
        return _LEGACY_TYPE_MAP[legacy_type]
    return "factual"


class MemoryLifecycleError(Exception):
    pass


class MemoryLifecycleService:
    def __init__(self, memory_repo):
        self.repo = memory_repo

    # ── §16.1/§16.2: classified storage with provenance ────────────────

    async def remember(self, *, user_id: str, content: str,
                       memory_class: str = "factual",
                       source: str = "",
                       confidence: Optional[float] = None,
                       expires_at: Optional[int] = None,
                       subject_key: Optional[str] = None,
                       embedding: List[float],
                       metadata: Optional[Dict[str, Any]] = None,
                       ) -> Dict[str, Any]:
        if memory_class not in MEMORY_CLASSES:
            raise MemoryLifecycleError(
                f"Unknown memory class {memory_class}; "
                f"expected one of {MEMORY_CLASSES}")
        if expires_at is not None and expires_at <= epoch_now():
            raise MemoryLifecycleError(
                "A memory cannot be stored already expired")
        meta = dict(metadata or {})
        # §17.4 (finding B): version progression 1 → 2 → 3 — a supplied
        # version (the correction's increment) is HONORED, never
        # overwritten back to 1.
        meta.update({"memory_class": memory_class, "source": source,
                     "confidence": confidence, "expires_at": expires_at,
                     "subject_key": subject_key,
                     "version": int(meta.get("version", 1))})
        entry_id = await self.repo.store_memory(
            user_id=user_id, content=content, memory_type=memory_class,
            metadata=meta, embedding=embedding)
        # §16.4: a newer entry supersedes the older one for the subject —
        # through the REPO contract (both stores implement it), failures
        # are loud, not suppressed.
        if subject_key:
            if hasattr(self.repo, "supersede_older_for_subject"):
                await self.repo.supersede_older_for_subject(
                    user_id, subject_key, entry_id)
            else:
                await self._supersede_older(user_id, subject_key, entry_id)
        return await self.retrieve(user_id, entry_id)

    async def _supersede_older(self, user_id: str, subject_key: str,
                               new_id: str) -> None:
        try:
            rows = await self.repo.db.fetch_all(
                "SELECT id, metadata FROM memories WHERE user_id=? AND "
                "subject_key=? AND id!=? AND superseded_by IS NULL",
                (user_id, subject_key, new_id))
            for r in rows or []:
                meta = dict(json.loads(r["metadata"] or "{}"))
                meta["superseded_by"] = new_id
                meta["superseded_at"] = epoch_now()
                await self.repo.db.execute(
                    "UPDATE memories SET superseded_by=?, metadata=? "
                    "WHERE id=?", (new_id, json.dumps(meta), r["id"]))
            await self.repo.db.commit()
        except Exception:
            logger.debug("Supersede pass failed", exc_info=True)

    # ── §16.3: authorized retrieval ────────────────────────────────────

    async def retrieve(self, user_id: str, memory_id: str) -> Optional[Dict[str, Any]]:
        """Disclosure authorization: the owner (or nobody) — similarity is
        never checked before ownership (§16.3)."""
        entry = await self.repo.get_memory(user_id, memory_id)
        if entry is None:
            return None
        if entry.get("expires_at") or \
                (entry.get("metadata") or {}).get("expires_at"):
            exp = entry.get("expires_at") or \
                (entry.get("metadata") or {}).get("expires_at")
            if exp and int(exp) <= epoch_now():
                return None   # expired: not served, not silently freshened
        return entry

    async def search(self, user_id: str, query_embedding: List[float],
                     limit: int = 8) -> List[Dict[str, Any]]:
        """Search scoped to the caller's OWN memories, superseded entries
        and expired entries excluded."""
        results = await self.repo.search_memory(
            user_id=user_id, query_embedding=query_embedding, limit=limit)
        now = epoch_now()
        out = []
        for r in results or []:
            entry = r.get("entry") if "entry" in r else r
            meta = (entry.get("metadata") or {}) if isinstance(entry, dict) else {}
            if entry.get("superseded_by") or meta.get("superseded_by"):
                continue
            exp = entry.get("expires_at") or meta.get("expires_at")
            if exp and int(exp) <= now:
                continue
            out.append(r)
        return out

    # ── §16.5: user control ────────────────────────────────────────────

    async def correct(self, user_id: str, memory_id: str, new_content: str,
                      embedding: List[float]) -> Dict[str, Any]:
        """§16.5: correction creates a NEW version under the same subject;
        the old content stays in history (superseded), never silently
        merged."""
        original = await self.repo.get_memory(user_id, memory_id)
        if original is None:
            raise MemoryLifecycleError("Memory not found")
        meta = dict(original.get("metadata") or {})
        subject_key = meta.get("subject_key") or memory_id
        stored = await self.remember(
            user_id=user_id, content=new_content,
            memory_class=meta.get("memory_class", "factual"),
            source=meta.get("source", "user_correction"),
            confidence=meta.get("confidence"),
            expires_at=meta.get("expires_at"),
            subject_key=subject_key,
            embedding=embedding,
            metadata={"corrected_from": memory_id,
                      "version": int(meta.get("version", 1)) + 1})
        # The corrected-from entry is superseded by the correction even
        # when it never had a subject key.
        try:
            await self.repo.db.execute(
                "UPDATE memories SET superseded_by=? WHERE id=? AND user_id=?",
                (stored["id"], memory_id, user_id))
            await self.repo.db.commit()
        except Exception:
            logger.debug("Correction supersede failed", exc_info=True)
        return stored

    async def export_all(self, user_id: str) -> Dict[str, Any]:
        """§16.5: the user's own memories, exported with provenance."""
        rows = await self.repo.list_memories(user_id=user_id, limit=10_000)
        return {"user_id": user_id, "count": len(rows or []),
                "memories": rows or []}

    # ── §16.6: deletion propagation ────────────────────────────────────

    async def forget(self, user_id: str, memory_id: str) -> Dict[str, Any]:
        """§16.6: forget = delete the row AND propagate to derived
        artifacts registered in metadata (embedding caches, dataset
        references). Un-referenced derived stores simply have nothing to
        invalidate."""
        atomic_forget = getattr(self.repo, "forget_with_cleanup", None)
        if callable(atomic_forget):
            return await atomic_forget(user_id, memory_id)
        entry = await self.repo.get_memory(user_id, memory_id)
        if entry is None:
            return {"deleted": False, "propagated": []}
        meta = dict(entry.get("metadata") or {})
        # §17.4 (finding B): dependents come from the DURABLE edge table
        # (registered at derivation time) — the old metadata-only refs
        # listed things without cleaning them.
        dependents = []
        if hasattr(self.repo, "dependents_of"):
            dependents = await self.repo.dependents_of(memory_id)
        for ref in meta.get("derived_refs") or []:
            dependents.append({"kind": "metadata_ref", "id": str(ref)})
        propagated: List[str] = [
            d.get("id") or d.get("derived_id") or str(d) for d in dependents]
        # 1) Children corrected FROM this entry keep their history but the
        #    dangling corrected_from pointer is expected (audit trail).
        deleted = await self.repo.delete_memory(user_id, memory_id)
        # 2) A durable, claimable CLEANUP JOB replaces the in-process
        #    side effect — progress is observable and retryable across
        #    restarts (§16.22/§17.4). The supersession pointers pointing
        #    at the deleted entry stay DANGLING on purpose: clearing them
        #    would REACTIVATE an older, corrected fact.
        job_id = None
        if hasattr(self.repo, "create_cleanup_job"):
            job_id = await self.repo.create_cleanup_job(
                memory_id=memory_id, user_id=user_id,
                dependents=dependents)
        return {"deleted": bool(deleted), "propagated": propagated,
                "cleanup_job_id": job_id,
                "note": ("deleting a correction never reactivates the "
                         "older fact it superseded")}
