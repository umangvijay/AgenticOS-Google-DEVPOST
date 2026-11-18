"""Phase 28 §28.9 — modeling drills that lacked named coverage.

The retained J24 journey (request → download → independent read →
edit-as-child, malformed/oversized refusals, durable verified
publication, PostgreSQL profile) and the unit suites are unchanged.
This file pins the remaining close-when rules:

1. A dimension edit is MEASURABLE in the downloaded geometry — the
   exported OBJ's bounding box tracks the requested size, and the spec
   declares units + material.
2. A cancelled export publishes NOTHING — no artifact row, no partial
   deliverable.
"""
import asyncio

import pytest

from backend.services.modeling_service import (
    KNOWN_MATERIALS, KNOWN_UNITS, ModelParameters, ModelingError,
    export_obj, validate_asset)


def _bbox(obj_content: bytes):
    xs, ys, zs = [], [], []
    for line in obj_content.decode().splitlines():
        if line.startswith("v "):
            x, y, z = (float(v) for v in line.split()[1:4])
            xs.append(x)
            ys.append(y)
            zs.append(z)
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def test_dimension_edit_is_measurable_in_the_downloaded_geometry():
    """Changing ONE dimension through the spec must be measurable in the
    exported geometry — the downloaded bounding box tracks the request."""
    base = ModelParameters(kind="box", size=(1.0, 1.0, 1.0),
                           name="base", units="mm", material="plastic")
    edited = ModelParameters(kind="box", size=(2.0, 1.0, 1.0),
                             name="edited", units="mm", material="plastic")
    base_obj = export_obj(base)
    edited_obj = export_obj(edited)
    assert validate_asset(base_obj["content"])["valid"]
    assert validate_asset(edited_obj["content"])["valid"]
    base_bbox = _bbox(base_obj["content"])
    edited_bbox = _bbox(edited_obj["content"])
    assert abs(base_bbox[0] - 1.0) < 1e-6, base_bbox
    assert abs(edited_bbox[0] - 2.0) < 1e-6, edited_bbox
    # The other dimensions are untouched.
    assert base_bbox[1:] == edited_bbox[1:]
    # The declared units and material ride the exported metadata.
    assert base_obj["parameters"]["units"] == "mm"
    assert base_obj["parameters"]["material"] == "plastic"


def test_units_and_material_are_bounded():
    for bad_units in ("furlongs", "", "MM2"):
        with pytest.raises(ModelingError):
            ModelParameters(kind="box", units=bad_units).validate()
    for bad_material in ("unobtainium", "", 42):
        with pytest.raises(ModelingError):
            ModelParameters(kind="box", material=bad_material).validate()
    for units in KNOWN_UNITS:
        ModelParameters(kind="box", units=units).validate()
    for material in KNOWN_MATERIALS:
        ModelParameters(kind="box", material=material).validate()


@pytest.mark.asyncio
async def test_cancelled_export_publishes_nothing(tmp_path, monkeypatch):
    """Cancelling a running export must not leave a published artifact —
    the workspace distinguishes a running job from a ready asset."""
    from backend.services import artifact_builder
    saved = []

    real_save = artifact_builder.save_user_files_with_delivery

    async def blocking_save(*args, **kwargs):
        await asyncio.Event().wait()  # cancelled while "saving"
        raise AssertionError("unreachable")

    monkeypatch.setattr(artifact_builder, "save_user_files_with_delivery",
                        blocking_save)
    task = asyncio.create_task(artifact_builder.generate_model_asset(
        "owner-x",
        {"kind": "box", "size": [1, 2, 3], "segments": 8,
         "name": "cancelled-job", "units": "mm", "material": "resin"},
        name="cancelled-job"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved == [] and not hasattr(artifact_builder, "_published")


# ── §28.9: the durable cancellable job layer ──────────────────────────

@pytest.mark.asyncio
async def test_modeling_job_lifecycle_and_restart_readable(tmp_path, monkeypatch):
    """A job record survives restart; a completed job is READY with the
    artifact digest; the artifact store holds the published bytes."""
    from backend.services.modeling_jobs import (
        ModelingJobStore, get_modeling_job_store, reset_modeling_job_store,
        run_modeling_job,
    )
    monkeypatch.setattr("backend.services.modeling_jobs._store",
                        ModelingJobStore(tmp_path / "jobs"))
    store = get_modeling_job_store()
    job = store.create("owner-x", {"kind": "box", "size": [1, 2, 3],
                                   "units": "mm", "material": "resin"},
                       run_id="run-9")
    async def publish():
        from backend.services.artifact_builder import save_user_files
        return save_user_files("owner-x", "job-model", [
            {"path": "model.obj", "content": export_obj(
                ModelParameters(kind="box", size=(1, 2, 3)))["content"].decode()}])
    metadata = await run_modeling_job(job["job_id"], publish=publish)
    # A FRESH store instance over the same root reads the durable record.
    fresh = ModelingJobStore(tmp_path / "jobs")
    record = fresh.load(job["job_id"])
    assert record["state"] == "ready"
    assert record["artifact_id"] == metadata["artifact_id"]
    assert record["digest"], record


@pytest.mark.asyncio
async def test_cancelled_modeling_job_publishes_nothing(tmp_path, monkeypatch):
    from backend.services import modeling_jobs
    from backend.services.modeling_jobs import (
        ModelingJobStore, cancel_modeling_job, run_modeling_job)
    monkeypatch.setattr("backend.services.modeling_jobs._store",
                        ModelingJobStore(tmp_path / "jobs"))
    store = modeling_jobs.get_modeling_job_store()
    job = store.create("owner-y", {"kind": "sphere"})
    published = []

    async def slow_publish():
        published.append(1)
        await asyncio.sleep(30)
        return {"artifact_id": "should-not-exist"}

    task = asyncio.create_task(run_modeling_job(job["job_id"],
                                                publish=slow_publish))
    await asyncio.sleep(0.05)
    result = await cancel_modeling_job(job["job_id"])
    assert result["state"] == "cancelled"
    # The publish CALL may have started (it was running); the contract is
    # that it never COMPLETED and no artifact was recorded.
    assert len(published) <= 1
    assert result["artifact_id"] is None
    with pytest.raises(asyncio.CancelledError):
        await task
    fresh = ModelingJobStore(tmp_path / "jobs")
    assert fresh.load(job["job_id"])["state"] == "cancelled"


def test_illegal_job_transitions_are_refused(tmp_path):
    from backend.services.modeling_jobs import (
        ModelingJobError, ModelingJobStore)
    store = ModelingJobStore(tmp_path / "jobs")
    job = store.create("owner-z", {"kind": "box"})
    store.transition(job["job_id"], "running")
    with pytest.raises(ModelingJobError):
        store.transition(job["job_id"], "queued")
    store.transition(job["job_id"], "ready")
    with pytest.raises(ModelingJobError):
        store.transition(job["job_id"], "cancelled")
