"""
AgentOS — 3D model-asset generation API (Phase 27 §27.15 Part B)

POST /api/v1/modeling/assets — generate a validated, downloadable OBJ
asset from a bounded parameter set. The request is a REAL modeling job:
parameters → mesh → independent byte validation → durable deliverable
publication → owner-authorized download through the artifacts API.
A modeling request never falls back to website generation.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional, Tuple

from backend.api.dependencies.auth import AuthenticatedUser, get_current_user
from backend.services.artifact_builder import ArtifactError, generate_model_asset
from backend.services.modeling_jobs import (
    ModelingJobError, cancel_modeling_job, get_modeling_job_store,
    run_modeling_job,
)

router = APIRouter(prefix="/modeling", tags=["modeling"])


class ModelAssetRequest(BaseModel):
    kind: str = Field(..., description="box | sphere | cylinder | assembly")
    size: List[float] = Field(default=[1.0, 1.0, 1.0], min_length=1, max_length=3)
    segments: int = Field(default=16, ge=3, le=256)
    parts: Optional[List[Dict[str, Any]]] = None
    name: str = Field(default="asset", max_length=60)
    units: str = Field(default="mm", max_length=8,
                       description="mm | cm | m | in — recorded in the asset metadata")
    material: str = Field(default="generic", max_length=20,
                          description="generic | plastic | aluminium | steel | wood | resin")
    run_id: Optional[str] = Field(default=None, max_length=120)


@router.post("/assets")
async def create_model_asset(
    request: ModelAssetRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_modeling_job_store()
    job = store.create(user.user_id, {
        "kind": request.kind, "size": list(request.size),
        "segments": request.segments, "parts": request.parts or [],
        "name": request.name, "units": getattr(request, "units", "mm"),
        "material": getattr(request, "material", "generic"),
    }, run_id=getattr(request, "run_id", None))

    async def _publish():
        return await generate_model_asset(
            user.user_id,
            {
                "kind": request.kind,
                "size": tuple(request.size),
                "segments": request.segments,
                "parts": request.parts or [],
                "name": request.name,
                "units": request.units,
                "material": request.material,
            },
                name=request.name,
                run_id=request.run_id,
            )

    try:
        metadata = await run_modeling_job(job["job_id"], publish=_publish)
    except ArtifactError as exc:
        # Bounded admission: malformed geometry, unsupported kinds and
        # oversized jobs are refused with an explicit client error.
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "artifact_id": metadata.get("artifact_id"),
        "name": metadata.get("name"),
        "files": [f.get("path") for f in metadata.get("files", [])],
        "model": metadata.get("model"),
        "delivery": metadata.get("delivery"),
        "job_id": job["job_id"],
        "job_state": "ready",
    }


class ModelAssetEditRequest(BaseModel):
    """30.40: a versioned edit — changed parameters produce a CHILD job
    (version N+1) whose artifact is independently validated; the parent
    revision stays downloadable."""
    size: Optional[List[float]] = Field(default=None, min_length=1, max_length=3)
    segments: Optional[int] = Field(default=None, ge=3, le=256)
    name: Optional[str] = Field(default=None, max_length=60)
    material: Optional[str] = Field(default=None, max_length=20)
    run_id: Optional[str] = Field(default=None, max_length=120)


@router.post("/jobs/{job_id}/edit")
async def edit_model_asset(
    job_id: str,
    request: ModelAssetEditRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_modeling_job_store()
    parent = store.load(job_id)
    if parent is None or parent.get("owner_user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="job not found")
    if parent.get("state") != "ready" or not parent.get("artifact_id"):
        raise HTTPException(status_code=409, detail=(
            "only a ready, published job can be edited; this job is "
            f"{parent.get('state')}"))
    overrides = {k: v for k, v in {
        "size": list(request.size) if request.size is not None else None,
        "segments": request.segments,
        "name": request.name,
        "material": request.material,
    }.items() if v is not None}
    if not overrides:
        raise HTTPException(status_code=400, detail="an edit must change at least one parameter")
    parameters = {**parent.get("parameters", {}), **overrides}
    child = store.create(
        user.user_id, parameters, run_id=request.run_id or parent.get("run_id"),
        parent_job_id=job_id, version=int(parent.get("version", 1)) + 1,
    )

    async def _publish():
        return await generate_model_asset(
            user.user_id,
            {
                "kind": parameters.get("kind", "box"),
                "size": tuple(parameters.get("size", [1.0, 1.0, 1.0])),
                "segments": int(parameters.get("segments", 16)),
                "parts": parameters.get("parts") or [],
                "name": parameters.get("name", "asset"),
                "units": parameters.get("units", "mm"),
                "material": parameters.get("material", "generic"),
            },
            name=parameters.get("name", "asset"),
            run_id=request.run_id or parent.get("run_id"),
        )

    try:
        metadata = await run_modeling_job(child["job_id"], publish=_publish)
    except ArtifactError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ModelingJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "artifact_id": metadata.get("artifact_id"),
        "name": metadata.get("name"),
        "files": [f.get("path") for f in metadata.get("files", [])],
        "model": metadata.get("model"),
        "delivery": metadata.get("delivery"),
        "job_id": child["job_id"],
        "parent_job_id": job_id,
        "version": child.get("version"),
        "job_state": "ready",
    }


@router.get("/jobs/{job_id}/versions")
async def list_model_job_versions(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_modeling_job_store()
    record = store.load(job_id)
    if record is None or record.get("owner_user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="job not found")
    versions = [
        {"job_id": j["job_id"], "version": j.get("version", 1),
         "state": j.get("state"), "artifact_id": j.get("artifact_id"),
         "digest": j.get("digest"), "parameters": j.get("parameters"),
         "created_at": j.get("created_at")}
        for j in store.lineage(job_id)
    ]
    return {"job_id": job_id, "versions": versions,
            "current_version": record.get("version", 1)}


@router.get("/jobs/{job_id}")
async def get_modeling_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    record = get_modeling_job_store().load(job_id)
    if record is None or record.get("owner_user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="job not found")
    return record


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_modeling_job_store()
    record = store.load(job_id)
    if record is None or record.get("owner_user_id") != user.user_id:
        raise HTTPException(status_code=404, detail="job not found")
    try:
        return await cancel_modeling_job(job_id)
    except ModelingJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
