"""
AgentOS — Generated artifacts API

GET  /api/v1/artifacts                 — List this user's generated websites/apps
GET  /api/v1/artifacts/{id}            — Artifact metadata
GET  /api/v1/artifacts/{id}/files/{path}?disposition=preview|download
                                       — Serve a generated file.

§28.8: preview and download are TWO actions over the SAME immutable
stored object — identical bytes, different Content-Disposition. Preview
serves inline (the dock viewer); download serves attachment with a safe
filename. Both are owner-authorized, both carry private cache controls,
and legacy clients (no disposition parameter) keep the download default.
"""

from urllib.parse import quote as _urlquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.services.artifact_builder import ArtifactError, list_artifacts, load_artifact, read_artifact_file

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

# §28.8: MIME by extension — an honest content type for the dock viewer;
# everything unrecognized stays octet-stream.
_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".obj": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}
_INLINE_TYPES = {"application/pdf", "text/plain", "text/markdown",
                 "text/csv", "text/html", "image/png", "image/jpeg",
                 "image/svg+xml"}


def _content_type_for(path_name: str) -> str:
    lower = path_name.lower()
    for suffix, mime in _MIME_BY_SUFFIX.items():
        if lower.endswith(suffix):
            return mime
    return "application/octet-stream"


def _file_response(*, path_name: str, local_path, disposition: str) -> Response:
    """The SAME bytes either way; only the delivery differs. Private
    cache controls on both actions; inline NEVER carries a filename that
    invites saving, download always carries a safe one."""
    media_type = _content_type_for(path_name)
    if disposition == "preview":
        inline = media_type in _INLINE_TYPES
        headers = {
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{_urlquote(path_name)}"
                if inline else
                f"attachment; filename*=UTF-8''{_urlquote(path_name)}"),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        return FileResponse(local_path, media_type=media_type, headers=headers)
    safe_name = "".join(
        c if (c.isalnum() or c in "._- ") else "_" for c in path_name) or "artifact"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{_urlquote(safe_name)}",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(local_path, media_type=media_type,
                        filename=safe_name, headers=headers)


@router.get("")
async def list_user_artifacts(user: AuthenticatedUser = Depends(get_current_user)):
    items = list_artifacts(user.user_id)
    return {"artifacts": items, "count": len(items)}


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    try:
        return load_artifact(user.user_id, artifact_id)
    except ArtifactError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{artifact_id}/files/{file_path:path}")
async def get_artifact_file(
    artifact_id: str,
    file_path: str,
    disposition: str = Query(default="download", pattern="^(preview|download)$"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        path = read_artifact_file(user.user_id, artifact_id, file_path)
    except ArtifactError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _file_response(path_name=path.name, local_path=path,
                          disposition=disposition)
