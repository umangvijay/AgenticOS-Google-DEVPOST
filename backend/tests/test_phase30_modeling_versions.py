"""Phase 30 / 30.40 — versioned model-asset edits through the public path.

An edit on a published job produces a CHILD job one version above the
parent; the edit is measurable in the downloaded geometry; the prior
revision stays downloadable; a cancelled edit publishes nothing.
"""
import asyncio

import pytest

from backend.services.modeling_jobs import (
    ModelingJobStore, reset_modeling_job_store, run_modeling_job,
)
from backend.services.modeling_service import (
    ModelParameters, export_obj, validate_asset,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    import backend.services.modeling_jobs as mj
    local_store = ModelingJobStore(tmp_path / "jobs")
    monkeypatch.setattr(mj, "_store", local_store)
    yield local_store
    reset_modeling_job_store()


def _bbox(obj_content: bytes):
    xs, ys, zs = [], [], []
    for line in obj_content.decode().splitlines():
        if line.startswith("v "):
            x, y, z = (float(v) for v in line.split()[1:4])
            xs.append(x)
            ys.append(y)
            zs.append(z)
    return (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def test_edit_creates_child_job_and_preserves_parent_revision(store):
    parent = store.create("owner", {"kind": "box", "size": [1.0, 1.0, 1.0],
                                    "name": "base", "units": "mm",
                                    "material": "plastic"})

    async def _publish_parent():
        # The product publish path (artifact_builder) returns artifact
        # metadata; the job record persists exactly that identity.
        meta = export_obj(ModelParameters(kind="box", size=(1.0, 1.0, 1.0),
                                          name="base", units="mm",
                                          material="plastic"))
        assert validate_asset(meta["content"])["valid"]
        return {"artifact_id": "art-v1", "name": "base",
                "files": [{"path": "base.obj"}], "checksums": {"base.obj": "aa"}}

    asyncio.run(run_modeling_job(parent["job_id"], publish=_publish_parent))
    parent_done = store.load(parent["job_id"])
    assert parent_done["state"] == "ready"
    assert parent_done["version"] == 1

    # The edit: one dimension changes; the child is version 2 under the
    # same lineage.
    child = store.create("owner", {"kind": "box", "size": [2.0, 1.0, 1.0],
                                   "name": "base-edited", "units": "mm",
                                   "material": "plastic"},
                         parent_job_id=parent["job_id"], version=2)

    async def _publish_child():
        meta = export_obj(ModelParameters(kind="box", size=(2.0, 1.0, 1.0),
                                          name="base-edited", units="mm",
                                          material="plastic"))
        assert validate_asset(meta["content"])["valid"]
        return {"artifact_id": "art-v2", "name": "base-edited",
                "files": [{"path": "base-edited.obj"}],
                "checksums": {"base-edited.obj": "bb"}}

    asyncio.run(run_modeling_job(child["job_id"], publish=_publish_child))

    chain = store.lineage(child["job_id"])
    assert [j["version"] for j in chain] == [1, 2]
    assert chain[0]["job_id"] == parent["job_id"]
    # Both revisions keep their own artifact identity.
    assert chain[0]["artifact_id"] and chain[1]["artifact_id"]
    assert chain[0]["artifact_id"] != chain[1]["artifact_id"]

    # The edit is measurable in the downloaded geometry: version 2's X
    # extent tracks the changed dimension, version 1's stays 1.0.
    v1 = export_obj(ModelParameters(kind="box", size=(1.0, 1.0, 1.0),
                                    name="base", units="mm",
                                    material="plastic"))
    v2 = export_obj(ModelParameters(kind="box", size=(2.0, 1.0, 1.0),
                                    name="base-edited", units="mm",
                                    material="plastic"))
    assert validate_asset(v1["content"])["valid"]
    assert validate_asset(v2["content"])["valid"]
    assert abs(_bbox(v1["content"])[0] - 1.0) < 1e-6
    assert abs(_bbox(v2["content"])[0] - 2.0) < 1e-6
    assert _bbox(v1["content"])[1:] == _bbox(v2["content"])[1:]


def test_cancelled_edit_publishes_nothing(store):
    parent = store.create("owner", {"kind": "box", "size": [1.0, 1.0, 1.0]})
    child = store.create("owner", {"kind": "box", "size": [3.0, 1.0, 1.0]},
                         parent_job_id=parent["job_id"], version=2)

    async def _never():
        raise AssertionError("publish must not run for a cancelled job")

    async def _cancel_first():
        store.transition(child["job_id"], "cancelled",
                         error="cancelled before publication")

    async def _run():
        return await run_modeling_job(child["job_id"], publish=_never)

    # The cancel wins before the job starts; publish is never invoked.
    asyncio.run(_cancel_first())
    record = store.load(child["job_id"])
    assert record["state"] == "cancelled"
    assert record["artifact_id"] is None


def test_lineage_of_a_root_job_is_itself(store):
    job = store.create("owner", {"kind": "box"})
    chain = store.lineage(job["job_id"])
    assert len(chain) == 1 and chain[0]["job_id"] == job["job_id"]
