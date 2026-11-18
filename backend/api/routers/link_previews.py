"""§28.7 — the chat link-preview route.

POST /api/v1/link-previews {url} → one bounded, anonymous metadata card
for a PUBLIC destination, or a per-request authorized card for a PRIVATE
workspace descriptor. Hovering a chat link creates NO managed browser
session, NO model usage, NO build and NO tool execution — this route
performs at most one bounded anonymous GET and nothing else.

Private descriptors are never cached and never fetched by this service;
they are authorized per request against the caller's own resources.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.services.link_preview_service import (
    LinkPreviewError,
    authorized_private_descriptor,
    get_link_preview_service,
)

router = APIRouter(prefix="/link-previews", tags=["link-previews"])


class LinkPreviewRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    # A private descriptor names a workspace-owned resource (e.g. an
    # artifact path); when true, the card is authorized per request and
    # never cached or fetched anonymously.
    private: bool = Field(default=False)


@router.post("")
async def create_link_preview(
    request: LinkPreviewRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    service = get_link_preview_service()
    if request.private:
        try:
            card = authorized_private_descriptor(
                request.url, grant_check=lambda url: _artifact_grant_check(
                    user.user_id, url))
        except LinkPreviewError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        return card
    try:
        # 30.38: the cache entry is partitioned under THIS caller's
        # identity — another account never gets this account's card.
        preview = await service.preview(request.url, principal=user.user_id)
    except LinkPreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return preview.to_dict()


def _artifact_grant_check(requester_id: str, url: str) -> bool:
    """Private descriptors carry their resource identity in the URL path
    (/api/v1/artifacts/<id>/...). Authorization is evaluated HERE per
    request through the artifact store's own ownership contract
    (`load_artifact` refuses a non-owner)."""
    import re
    match = re.search(r"/artifacts/([^/?#]+)", url or "")
    if not match:
        return False
    artifact_id = match.group(1)
    try:
        from backend.services.artifact_builder import load_artifact
        load_artifact(requester_id, artifact_id)
    except Exception:  # noqa: BLE001 — anything but an owner read is a refusal
        return False
    return True


@router.delete("/cache")
async def invalidate_preview(
    url: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, bool]:
    get_link_preview_service().invalidate(url, principal=user.user_id)
    return {"invalidated": True}
