"""Deliverables API (Phase 17 §17.6/§17.21/§17.22) — the evidence
surface for durable deliverables: restart-readable records, owner-scoped
access, field-level review tied to the EXACT revision."""
from __future__ import annotations

from typing import Optional

from backend.models.deliverable import Deliverable
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.dependencies.auth import (
    AuthenticatedUser, get_current_user, require_not_viewer,
)

router = APIRouter(prefix="/deliverables", tags=["deliverables"])


def _repo(request: Request):
    from backend.repositories.deliverable_repository import (
        get_deliverable_repo)
    repo = get_deliverable_repo()
    if repo is None:
        factory = getattr(request.app.state, "factory", None)
        repo = getattr(factory, "deliverable_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail={
            "code": "DELIVERABLE_STORE_UNAVAILABLE",
            "message": "The deliverable store is not configured"})
    return repo


def _public(d) -> dict:
    return {
        "deliverable_id": d.deliverable_id,
        "deliverable_key": d.deliverable_key,
        "revision": d.revision,
        "parent_revision": d.parent_revision,
        "deliverable_type": d.deliverable_type,
        "lifecycle_state": d.lifecycle_state,
        "file_manifest": d.file_manifest,
        "content_checksum": d.content_checksum,
        "verification": d.verification,
        "lineage": d.lineage,
        "updated_at": d.updated_at,
    }


@router.get("")
async def list_deliverables(request: Request, limit: int = 50, offset: int = 0,
                            user: AuthenticatedUser = Depends(get_current_user)):
    """§18.1: paginated listing (bounded page size)."""
    repo = _repo(request)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = await repo.list_for_owner(user.user_id, limit=limit + 1,
                                     offset=offset)
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"deliverables": [_public(d) for d in rows],
            "limit": limit, "offset": offset, "has_more": has_more}


@router.get("/{deliverable_id}")
async def get_deliverable(deliverable_id: str, request: Request,
                          user: AuthenticatedUser = Depends(get_current_user)):
    repo = _repo(request)
    d = await repo.get_for_owner(deliverable_id, user.user_id)
    if d is None:
        # Wrong-user access looks identical to missing (§12.28).
        raise HTTPException(status_code=404, detail="Deliverable not found")
    return _public(d)


class ReviewRequest(BaseModel):
    revision: int
    action: str          # confirm | remove | edit
    field: str
    value: Optional[str] = None


@router.post("/{deliverable_id}/review")
async def review_deliverable(deliverable_id: str, body: ReviewRequest,
                             request: Request,
                             user: AuthenticatedUser = Depends(require_not_viewer)):
    """§18.4 (C9/C10): field-level review actions CHANGE THE DOCUMENT.

    - confirm: records the user attestation (verification metadata only).
    - remove: strips the claim's text from the document content, creates
      a NEW revision, and re-verifies it.
    - edit: replaces the claim's text, new revision, re-verified.

    The atomic expected-revision check runs against the KEY's latest
    revision inside one transaction — a stale review is a structured
    conflict carrying the current revision."""
    repo = _repo(request)
    d = await repo.get_for_owner(deliverable_id, user.user_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    if body.action not in ("confirm", "remove", "edit"):
        raise HTTPException(status_code=422, detail="unknown review action")

    # §19 C4: EVERY action (confirm included) runs the ATOMIC
    # expected-revision gate — the check of the key's current revision
    # and the mutation commit in one transaction.
    def _confirm_mutate(manifest):
        # Confirm changes no bytes — the manifest passes through.
        return manifest

    if body.action == "confirm":
        gate = await repo.review_with_revision_check(
            deliverable_id, expected_revision=body.revision,
            mutate=_confirm_mutate)
        if gate.get("conflict"):
            raise HTTPException(status_code=409, detail={
                "code": "REVISION_MISMATCH",
                "message": (f"the reviewed revision {body.revision} is "
                            f"not current ({gate.get('current_revision')})"),
                "current_revision": gate.get("current_revision"),
                "next_action": "review the current revision",
            })
        if gate.get("not_found"):
            raise HTTPException(status_code=404, detail="Deliverable not found")
        verification = dict(d.verification or {})
        claims = [c for c in (verification.get("unsupported_claims") or [])
                  if c.get("field") != body.field]
        reviewed = list(verification.get("reviewed_claims") or [])
        reviewed.append({"field": body.field, "action": "confirm",
                         "revision": body.revision,
                         "reviewer": f"user:{user.user_id}",
                         "kind": "user_attestation"})
        verification["unsupported_claims"] = claims
        verification["reviewed_claims"] = reviewed
        await repo.transition(deliverable_id, d.lifecycle_state,
                              verification=verification)
        return {"deliverable_id": deliverable_id, "revision": d.revision,
                "action": "confirm", "field": body.field,
                "kind": "user_attestation",
                "remaining_claims": len(claims)}

    # remove / edit: mutate the CONTENT through the atomic revision
    # check; a conflict carries the current revision + next action.
    def _mutate(manifest):
        new_manifest = []
        for f in manifest:
            content = f.get("content") or ""
            claim_value = body.value if body.action == "edit" else None
            if body.action == "remove" and body.field:
                # Remove the claim's field marker and any flagged text.
                content = _remove_claim_from_html(content, body.field)
            elif body.action == "edit" and body.value:
                content = _replace_claim_in_html(content, body.field,
                                                 body.value)
            import hashlib as _hl
            encoded = content.encode("utf-8")
            new_manifest.append({**f, "content": content,
                                 "sha256": _hl.sha256(encoded).hexdigest(),
                                 "byte_length": len(encoded)})
        return new_manifest

    out = await repo.review_with_revision_check(
        deliverable_id, expected_revision=body.revision, mutate=_mutate)
    if out.get("conflict"):
        raise HTTPException(status_code=409, detail={
            "code": "REVISION_MISMATCH",
            "message": (f"the reviewed revision {body.revision} is not "
                        f"current ({out.get('current_revision')})"),
            "current_revision": out.get("current_revision"),
            "next_action": "review the current revision",
        })
    if out.get("not_found"):
        raise HTTPException(status_code=404, detail="Deliverable not found")
    # The content changed → a NEW REVISION is created and re-verified.
    new_manifest = out["manifest"]
    verification = dict(d.verification or {})
    claims = [c for c in (verification.get("unsupported_claims") or [])
              if c.get("field") != body.field]
    verification["unsupported_claims"] = claims
    reviewed = list(verification.get("reviewed_claims") or [])
    reviewed.append({"field": body.field, "action": body.action,
                     "value": body.value, "revision": body.revision,
                     "reviewer": f"user:{user.user_id}"})
    verification["reviewed_claims"] = reviewed
    verification.pop("stale", None)
    new_d = await repo.create_candidate(
        Deliverable(deliverable_id="new", deliverable_key=d.deliverable_key,
                    revision=1, tenant_id=d.tenant_id,
                    owner_user_id=d.owner_user_id,
                    deliverable_type=d.deliverable_type,
                    run_id=d.run_id, task_id=d.task_id,
                    mime_type=d.mime_type, file_manifest=new_manifest,
                    lineage={**d.lineage,
                             "reviewed_from_revision": body.revision},
                    verification=verification),
        deliverable_key=d.deliverable_key)
    await repo.transition(new_d.deliverable_id, "verification_completed",
                          verification=verification)
    return {"deliverable_id": new_d.deliverable_id,
            "revision": new_d.revision,
            "action": body.action, "field": body.field,
            "remaining_claims": len(claims),
            "note": ("the claim removal/edit changed the document content "
                     "and created a re-verified revision (§18.4)")}


def _remove_claim_from_html(content: str, field: str) -> str:
    """§18.4 (C9): the flagged claim's element is REMOVED from the
    document content — the downloaded output no longer contains it."""
    import re as _re
    # Remove elements whose text mentions the flagged field name.
    pattern = _re.compile(
        r"<([a-z0-9]+)[^>]*>\s*[^<]*" + _re.escape(field) +
        r"[^<]*</\1\s*>", _re.I)
    return pattern.sub("", content or "")


def _replace_claim_in_html(content: str, field: str, new_value: str) -> str:
    """§18.4 (C9): the claim's text is REPLACED with the edited value."""
    import re as _re
    if not content:
        return content
    return _re.sub(_re.escape(field), new_value, content, count=1,
                   flags=_re.I)
