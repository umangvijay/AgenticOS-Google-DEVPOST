"""The ONE application-level memory service (Phase 17 §17.3, finding A).

Every entry point — HTTP creation/search, orchestrator memory tools,
specialist retrieval, automatic extraction, correction, export — goes
through THIS service. Authority derives from the AUTHENTICATED
execution context: the model cannot set ownership, verification,
sharing, or approval fields through metadata (those fields are
server-owned; metadata keys attempting them are stripped).

Retrieval applies the SAME visibility rules to semantic AND keyword
fallback: scope/grants, expiry, supersession, deletion barriers,
verification status, specialist limits — with provenance and
uncertainty labels KEPT in what the agent sees.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger("memory_service")

# §17.3: metadata keys the MODEL may never set (server-owned fields).
SERVER_OWNED_METADATA_KEYS = (
    "scope", "scope_ref", "verification_status", "sensitivity",
    "policy_decision", "policy_reason", "user_id", "owner",
    "deletion_state", "observed_at",
)


def strip_server_owned_fields(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """The model's metadata cannot grant itself visibility or status —
    the governed write policy decides those."""
    out = {k: v for k, v in (metadata or {}).items()
           if k not in SERVER_OWNED_METADATA_KEYS}
    removed = [k for k in (metadata or {}) if k in SERVER_OWNED_METADATA_KEYS]
    if removed:
        logger.info("Stripped server-owned metadata keys: %s", removed)
    return out


class ApplicationMemoryService:
    def __init__(self, memory_repo, *, lifecycle=None, dependency_map=None):
        self.repo = memory_repo
        self._lifecycle = lifecycle
        self._deps = dependency_map

    def _lifecycle_svc(self):
        if self._lifecycle is None:
            from backend.services.memory_lifecycle import MemoryLifecycleService
            self._lifecycle = MemoryLifecycleService(self.repo)
        return self._lifecycle

    # ── write ───────────────────────────────────────────────────────────

    async def remember(self, *, user_id: str, content: str,
                       memory_class: str = "factual", source: str = "",
                       explicit: bool = True,
                       metadata: Optional[Dict[str, Any]] = None,
                       embedding: Optional[List[float]] = None,
                       scope: str = "personal",
                       ) -> Dict[str, Any]:
        """§17.3: the governed write path used by EVERY entry point."""
        from backend.services.memory_governance import (
            MemoryCandidate, decide_write_policy, validate_candidate,
        )
        meta = strip_server_owned_fields(metadata or {})
        candidate = MemoryCandidate(
            content=content, memory_class=memory_class, scope=scope,
            source=source or f"user:{user_id}", explicit=explicit,
            observed_at=epoch_now(),
            source_event_id=meta.get("source_event_id"),
        )
        validate_candidate(candidate)
        decided = decide_write_policy(candidate)
        if decided.policy_decision == "refused":
            raise ValueError(decided.policy_reason)
        meta.update({
            "scope": decided.scope, "scope_ref": decided.scope_ref,
            "verification_status": decided.verification_status,
            "sensitivity": decided.sensitivity, "source": decided.source,
            "policy_decision": decided.policy_decision,
            "policy_reason": decided.policy_reason,
        })
        entry = await self._lifecycle_svc().remember(
            user_id=user_id, content=content, memory_class=memory_class,
            source=decided.source, subject_key=meta.get("subject_key"),
            embedding=embedding or [], metadata=meta)
        return {"entry": entry, "policy_decision": decided.policy_decision,
                "verification_status": decided.verification_status,
                "policy_reason": decided.policy_reason}

    # ── retrieval (semantic + keyword share the SAME rules) ─────────────

    async def retrieve_for_task(self, *, user_id: str, query: str,
                                query_embedding: Optional[List[float]] = None,
                                limit: int = 5,
                                specialist_role: Optional[str] = None,
                                memory_scope: Optional[Dict[str, Any]] = None,
                                ) -> Dict[str, Any]:
        """§17.3: semantic AND fallback retrieval apply the SAME
        visibility rules, through governed_search. Provenance and
        uncertainty labels stay attached to what the agent sees."""
        from backend.services.memory_governance import (
            classify_retrieved_content, governed_search,
            permitted_scopes_for, specialist_memory_filters,
            retrieval_abstention,
        )
        scopes = permitted_scopes_for(user_id=user_id, task_context={})
        if memory_scope is not None and not specialist_role:
            raise ValueError("A specialist memory scope requires its role")
        filters = specialist_memory_filters(specialist_role, memory_scope) if specialist_role is not None else None
        out = await governed_search(
            repo=self.repo,
            lifecycle=self._lifecycle_svc(),
            user_id=user_id, query=query,
            query_embedding=query_embedding or [], scopes=scopes,
            specialist_filters=filters, limit=limit)
        results = []
        for r in out["results"]:
            flagged = classify_retrieved_content(r["content"])
            results.append({
                "id": r["memory_id"], "content": r["content"],
                "memory_class": r.get("memory_class"),
                "verification_status": r.get("status"),
                "scope": r.get("scope"), "source": r.get("source"),
                "hostile_flag": flagged["hostile_flag"],
                "content_class": flagged["content_class"],
                "ranking": r.get("ranking"),
            })
        # §18.6 (finding C12-note): the abstention contract reads the
        # STATUS field — map the governed row's verification_status to
        # it (the constructed key and the read key must agree).
        ab = retrieval_abstention(
            [{"status": r.get("verification_status")
              or r.get("status")} for r in results])
        return {"results": results, "abstained": ab["abstained"],
                "abstention_reason": ab["reason"],
                "excluded": out["excluded"]}

    # ── correction / deletion / export via the durable workflows ───────

    async def correct(self, *, user_id: str, memory_id: str, content: str,
                      embedding: Optional[List[float]] = None) -> Dict[str, Any]:
        from backend.services.memory_workflows import propagate_correction
        out = await propagate_correction(
            memory_repo=self.repo, lifecycle=self._lifecycle_svc(),
            dependency_map=self._deps,
            user_id=user_id, memory_id=memory_id, new_content=content,
            new_embedding=embedding or [])
        # §18.6: correction ALSO propagates to deliverable invalidation —
        # a corrected source marks dependent verifications STALE (§17.24).
        try:
            if hasattr(self.repo, "dependents_of"):
                for dep in await self.repo.dependents_of(memory_id):
                    if (dep.get("derived_kind") or dep.get("kind")) == \
                            "delivered_artifact":
                        from backend.services.deliverable_publisher import (
                            propagate_source_change,
                        )
                        from backend.repositories.deliverable_repository import (
                            get_deliverable_repo,
                        )
                        drepo = get_deliverable_repo()
                        if drepo is not None:
                            await propagate_source_change(
                                memory_repo=self.repo,
                                deliverable_repo=drepo,
                                source_memory_id=memory_id,
                                change="corrected")
        except Exception:
            logger.debug("Correction deliverable propagation failed",
                         exc_info=True)
        return {"entry": out.get("correction"),
                "invalidated_dependents": out.get("invalidated_dependents", [])}

    async def forget(self, *, user_id: str, memory_id: str) -> Dict[str, Any]:
        out = await self._lifecycle_svc().forget(user_id, memory_id)
        if not out.get("deleted"):
            return out
        # §17.4: the durable cleanup job is claimable by the worker.
        if out.get("cleanup_job_id") is None and \
                hasattr(self.repo, "create_cleanup_job"):
            out["cleanup_job_id"] = await self.repo.create_cleanup_job(
                memory_id=memory_id, user_id=user_id,
                dependents=await self.repo.dependents_of(memory_id)
                if hasattr(self.repo, "dependents_of") else [])
        # §18.6 (C12): correction/deletion propagates to DELIVERABLE
        # invalidation through the durable dependency graph — the worker
        # cleanup pass cleans registered dependents; deliverable edges
        # are marked stale via the publisher's propagation contract.
        try:
            if hasattr(self.repo, "dependents_of"):
                for dep in await self.repo.dependents_of(memory_id):
                    if (dep.get("derived_kind") or dep.get("kind")) == \
                            "delivered_artifact":
                        from backend.services.deliverable_publisher import (
                            propagate_source_change,
                        )
                        from backend.repositories.deliverable_repository import (
                            get_deliverable_repo,
                        )
                        drepo = get_deliverable_repo()
                        if drepo is not None:
                            await propagate_source_change(
                                memory_repo=self.repo,
                                deliverable_repo=drepo,
                                source_memory_id=memory_id,
                                change="deleted")
        except Exception:
            logger.warning("Deliverable invalidation propagation failed — "
                           "stale verifications may not be marked",
                           exc_info=True)
        return out

    async def export_all(self, user_id: str) -> Dict[str, Any]:
        from backend.services.memory_governance import contains_secret
        import json as _json
        out = await self._lifecycle_svc().export_all(user_id)
        if contains_secret(_json.dumps(out, default=str)):
            raise ValueError(
                "export withheld: secret-shaped content detected (§16.4)")
        return out
