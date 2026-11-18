"""
AgentOS — Memory Router

POST /api/v1/memory             — Add a memory entry
GET  /api/v1/memory             — List memory entries
POST /api/v1/memory/search      — Semantic search
DELETE /api/v1/memory/{id}      — Delete a memory entry
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Depends, status
from pydantic import BaseModel, Field
from typing import Optional, List

from backend.api.dependencies.auth import get_current_user, AuthenticatedUser, require_not_viewer
from backend.security.input_sanitizer import sanitize_text, InputValidationError
from backend.services.embedding_service import embed_text_metered

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])

ALLOWED_MEMORY_TYPES = {"profile", "workflow", "semantic"}


def _get_factory(request: Request):
    factory = getattr(request.app.state, "factory", None)
    if not factory:
        raise HTTPException(status_code=500, detail="Server not initialized")
    return factory


class AddMemoryRequest(BaseModel):
    content: str
    memory_type: str = "semantic"   # "profile" | "workflow" | "semantic"
    tags: Optional[List[str]] = None
    metadata: Optional[dict] = None

class SearchMemoryRequest(BaseModel):
    query: str
    memory_type: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=100)


def _validate_memory_type(memory_type: Optional[str]) -> Optional[str]:
    if memory_type is None:
        return None
    if memory_type not in ALLOWED_MEMORY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"memory_type must be one of {sorted(ALLOWED_MEMORY_TYPES)}",
        )
    return memory_type


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_memory(
    body: AddMemoryRequest, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Add a memory entry with optional embedding."""
    factory = _get_factory(request)
    memory_type = _validate_memory_type(body.memory_type)

    try:
        content = sanitize_text(body.content, "content", max_length=10_000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    if not content.strip():
        raise HTTPException(status_code=422, detail="content cannot be empty")

    embedding = None
    try:
        from backend.services.embedding_service import GoogleCloudEmbeddingService
        embedding = await embed_text_metered(GoogleCloudEmbeddingService(), content)
    except Exception as e:
        logger.exception("Embedding generation failed")
        # Preserve content and report degraded retrieval honestly.
        embedding = None

    metadata = dict(body.metadata or {})
    if body.tags:
        metadata["tags"] = body.tags
    # ── Phase 16 finding A: the GOVERNED write path ────────────────────
    # Extraction proposes; the server decides (§16.3/§16.6). The write
    # policy refuses secrets (§16.4), refuses inference-borne org
    # visibility (§16.1), and marks inferred facts as needing
    # confirmation — the API surfaces the decision honestly.
    from backend.services.memory_governance import (
        MemoryCandidate, decide_write_policy, validate_candidate,
    )
    from backend.services.memory_lifecycle import normalize_memory_class
    candidate = MemoryCandidate(
        content=content,
        memory_class=normalize_memory_class(
            metadata.get("memory_class"), memory_type),
        scope=str(metadata.get("scope") or "personal"),
        scope_ref=str(metadata.get("scope_ref") or ""),
        source=str(metadata.get("source") or f"user:{user.user_id}"),
        explicit=bool(metadata.get("explicit", True)),
        expected_lifetime=str(metadata.get("expected_lifetime")
                              or "until_changed"),
        expires_at=metadata.get("expires_at"),
        observed_at=metadata.get("observed_at"),
        source_event_id=metadata.get("source_event_id"),
        sensitivity=str(metadata.get("sensitivity") or "normal"),
    )
    try:
        validate_candidate(candidate)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    decided = decide_write_policy(candidate)
    if decided.policy_decision == "refused":
        raise HTTPException(status_code=422, detail={
            "code": "MEMORY_POLICY_REFUSED",
            "message": decided.policy_reason,
        })
    metadata.update({
        "memory_class": decided.memory_class,
        "scope": decided.scope,
        "scope_ref": decided.scope_ref,
        "verification_status": decided.verification_status,
        "sensitivity": decided.sensitivity,
        "source": decided.source,
        "policy_decision": decided.policy_decision,
        "policy_reason": decided.policy_reason,
        "source_event_id": decided.source_event_id,
    })
    try:
        memory_id = await factory.memory_repo.store_memory(
            user_id=user.user_id,
            content=content,
            memory_type=memory_type,
            metadata=metadata,
            embedding=embedding or [],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("store_memory failed")
        raise HTTPException(
            status_code=500,
            detail=f"Could not store the memory entry on this storage backend: {type(e).__name__}",
        ) from e

    return {"memory_id": memory_id, "embedded": embedding is not None,
            "policy_decision": decided.policy_decision,
            "policy_reason": decided.policy_reason,
            "verification_status": decided.verification_status}


@router.get("")
async def list_memories(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    memory_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List memory entries for the current user."""
    factory = _get_factory(request)
    _validate_memory_type(memory_type)
    lister = getattr(factory.memory_repo, "list_memories", None)
    if not callable(lister):
        raise HTTPException(
            status_code=500,
            detail="Memory listing is not available on this storage backend.",
        )
    try:
        memories = await lister(
            user_id=user.user_id,
            memory_type=memory_type,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        logger.exception("list_memories failed")
        raise HTTPException(status_code=500, detail="Could not list memories") from e
    return {"memories": memories, "count": len(memories)}


@router.post("/search")
async def search_memory(
    body: SearchMemoryRequest, request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Semantic search over memory using embeddings."""
    factory = _get_factory(request)
    _validate_memory_type(body.memory_type)

    try:
        query = sanitize_text(body.query, "query", max_length=2000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)

    # ── Phase 16 finding A: the GOVERNED retrieval path ────────────────
    # Scope derives from the authenticated principal (§16.10); the
    # search applies the lifecycle filters BEFORE scoring, ranks with
    # separate signals, bounds the context, and abstains honestly
    # (§16.25) — every row is untrusted data with its hostile flag.
    from backend.services.memory_governance import (
        classify_retrieved_content, governed_search, permitted_scopes_for,
        retrieval_abstention,
    )
    try:
        from backend.services.embedding_service import GoogleCloudEmbeddingService
        query_embedding = await embed_text_metered(
            GoogleCloudEmbeddingService(), query) or []
    except Exception:
        query_embedding = []
    scopes = permitted_scopes_for(
        user_id=user.user_id, task_context={},
        org_shared_refs=list(getattr(user, "org_shared_refs", []) or []))
    from backend.services.memory_lifecycle import MemoryLifecycleService
    # §16.2 dual-read: the search filter is normalized through the SAME
    # function as the write path — a legacy memory_type spelling
    # ("semantic"→factual, "profile"→preference, "workflow"→
    # execution_episode) must read the rows the normalized writer
    # stored. Filtering by the raw spelling hid every such row.
    from backend.services.memory_lifecycle import normalize_memory_class
    governed = await governed_search(
        repo=factory.memory_repo,
        lifecycle=MemoryLifecycleService(factory.memory_repo),
        user_id=user.user_id, query=query,
        query_embedding=query_embedding, scopes=scopes,
        # Absence means all authorized classes, not the writer's default
        # factual class. Only an explicitly supplied type narrows retrieval.
        memory_class=(normalize_memory_class(None, body.memory_type)
                      if body.memory_type is not None else None),
        limit=body.limit)
    results = []
    for r in governed["results"]:
        hostile = classify_retrieved_content(r["content"])   # finding B
        results.append({**r, "hostile_flag": hostile["hostile_flag"]})
    ab = retrieval_abstention(results)
    return {"results": results, "count": len(results),
            "retrieval_mode": "governed",
            "abstained": ab["abstained"], "abstention_reason": ab["reason"],
            "excluded": governed["excluded"],
            "context_chars": governed["context_chars"]}


@router.get("/search")
async def search_memory_get(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    q: str = Query(..., min_length=1, max_length=2000),
    memory_type: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    """GET-form of memory search — consistent with the rest of the GET surface.

    The POST form stays the canonical rich-search endpoint (embedding mode);
    this alias runs the same pipeline with query parameters."""
    body = SearchMemoryRequest(query=q, memory_type=memory_type, limit=limit)
    return await search_memory(body, request, user)


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_not_viewer),
):
    """Delete a memory entry."""
    factory = _get_factory(request)
    deleted = await factory.memory_repo.delete_memory(user.user_id, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"memory_id": memory_id, "deleted": True}


# ── Phase 16: memory lifecycle (§16.5/§16.6 user control) ──────────────

class CorrectMemoryRequest(BaseModel):
    content: str


def _lifecycle(request: Request):
    factory = _get_factory(request)
    svc = getattr(factory, "memory_lifecycle", None)
    if svc is None:
        raise HTTPException(status_code=503, detail={
            "code": "MEMORY_LIFECYCLE_UNAVAILABLE",
            "message": "Memory lifecycle is not configured"})
    return svc


@router.get("/export")
async def export_memories(request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    """§16.23 (finding H): the export includes provenance, status, and
    version history — and the §16.4 SECRET GATE runs on the export
    payload itself (defense in depth: a secret that somehow entered a
    row cannot leave through the export)."""
    out = await _lifecycle(request).export_all(user.user_id)
    from backend.services.memory_governance import contains_secret
    import json as _json
    body = _json.dumps(out, default=str)
    if contains_secret(body):
        # Refuse the whole export rather than leaking one row.
        raise HTTPException(status_code=422, detail={
            "code": "EXPORT_CONTAINS_SECRET_SHAPED_CONTENT",
            "message": ("the export was withheld: secret-shaped content "
                        "was detected (§16.4) — correct or delete the "
                        "affected memory"),
        })
    return out


@router.get("/effect")
async def memory_effect(request: Request,
                        user: AuthenticatedUser = Depends(get_current_user)):
    """§16.26 (finding K): memory's MEASURED effect on outcomes —
    with/without-memory comparisons on the same task population, with
    missing measurements staying missing. Read-only; the deployment
    supplies the measured windows."""
    if not user.is_admin():
        raise HTTPException(status_code=403, detail="Admin access required")
    from backend.services.memory_governance import evaluate_memory_effect
    state = getattr(request.app.state, "memory_effect_metrics", None) or {}
    return evaluate_memory_effect(baseline=state.get("baseline", {}),
                                  with_memory=state.get("with_memory", {}),
                                  ) if False else evaluate_memory_effect(
        baseline=state.get("baseline", {}),
        with_memory=state.get("with_memory", {}))


@router.post("/{memory_id}/correct")
async def correct_memory(memory_id: str, body: CorrectMemoryRequest,
                         request: Request,
                         user: AuthenticatedUser = Depends(require_not_viewer)):
    """§16.5: correction creates a NEW version under the same subject —
    the old content stays in history, never silently merged."""
    from backend.services.embedding_service import GoogleCloudEmbeddingService
    factory = _get_factory(request)
    try:
        content = sanitize_text(body.content, "content", max_length=10_000)
    except InputValidationError as e:
        raise HTTPException(status_code=400, detail=e.message)
    # §17.2 (finding D): correction embeds through the METERED path —
    # and a failed embedding records "unavailable" honestly (no
    # fabricated placeholder vector that would poison similarity).
    embedding = None
    try:
        embedding = await embed_text_metered(
            GoogleCloudEmbeddingService(), content)
    except Exception:
        embedding = None
    # The correction's own row registers as a dependent of the OLD
    # version (finding I): correcting invalidates the superseded
    # record's retrievability while preserving history.
    try:
        _memory_dependency_map().register(
            memory_id, derived_kind="correction", derived_id=memory_id)
    except Exception:
        logger.debug("Dependent registration skipped", exc_info=True)
    try:
        stored = await _lifecycle(request).correct(
            user.user_id, memory_id, content, embedding)
    except Exception as exc:
        from backend.services.memory_lifecycle import MemoryLifecycleError
        if isinstance(exc, MemoryLifecycleError):
            raise HTTPException(status_code=404, detail=str(exc))
        raise
    return stored


@router.post("/{memory_id}/forget")
async def forget_memory(memory_id: str, request: Request,
                        user: AuthenticatedUser = Depends(require_not_viewer)):
    """§16.22 (finding C): deletion is a WORKFLOW — the marker creates
    an IMMEDIATE retrieval barrier (hidden), then cleanup is observable
    and retryable. Retention boundaries are stated, never implied."""
    factory = _get_factory(request)
    from backend.services.memory_workflows import request_deletion
    out = await request_deletion(
        memory_repo=factory.memory_repo, user_id=user.user_id,
        memory_id=memory_id,
        dependency_map=_memory_dependency_map())
    if not out.get("requested"):
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"memory_id": memory_id, **out}


@router.post("/{memory_id}/cleanup")
async def cleanup_memory(memory_id: str, request: Request,
                         user: AuthenticatedUser = Depends(require_not_viewer)):
    """§16.22 (finding C): the retryable cleanup step — after the
    dependents are invalidated the row is removed and the state recorded."""
    factory = _get_factory(request)
    from backend.services.memory_workflows import complete_deletion_cleanup
    out = await complete_deletion_cleanup(
        memory_repo=factory.memory_repo, user_id=user.user_id,
        memory_id=memory_id)
    if not out.get("deleted"):
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"memory_id": memory_id, **out}


def _memory_dependency_map():
    """Finding I: the PROCESS-level dependency map — dependents
    registered at derivation time invalidate with their source."""
    global _DEPENDENCY_MAP
    if _DEPENDENCY_MAP is None:
        from backend.services.memory_workflows import DependencyMap
        _DEPENDENCY_MAP = DependencyMap()
    return _DEPENDENCY_MAP


_DEPENDENCY_MAP = None
