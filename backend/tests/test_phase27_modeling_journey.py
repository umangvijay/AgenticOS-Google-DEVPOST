"""Phase 27 §27.15 — J24: the real modeling journey through the HTTP API.

A user requests a model → the response carries a downloadable artifact →
the bytes are parsed by an INDEPENDENT reader (not modeling_service) →
a changed dimension changes the geometry as requested → the same spec
re-exports deterministically → malformed/oversized jobs are refused →
cross-user download is denied → publication produced a durable,
verified deliverable.
"""
import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.dependencies import auth as auth_deps
from backend.api.routers import artifacts as artifacts_router
from backend.api.routers import modeling as modeling_router
from backend.repositories.deliverable_repository import (
    DeliverableRepository, set_deliverable_repo, reset_deliverable_repo)
from backend.services.artifact_builder import artifacts_root


@pytest.fixture
def client(tmp_path, monkeypatch):
    import asyncio
    from backend.repositories.sqlite.database import DatabaseManager

    db = DatabaseManager(str(tmp_path / "deliverables.db"))
    asyncio.run(db.initialize())
    set_deliverable_repo(DeliverableRepository(db))
    monkeypatch.setattr(artifacts_root.__module__ + ".artifacts_root",
                        lambda: tmp_path / "artifacts")
    import backend.services.artifact_builder as ab
    monkeypatch.setattr(ab, "artifacts_root", lambda: tmp_path / "artifacts")

    app = FastAPI()
    app.include_router(modeling_router.router)
    app.include_router(artifacts_router.router)

    def actor(user_id: str):
        async def _override():
            return auth_deps.AuthenticatedUser(
                user_id=user_id, email=f"{user_id}@example.com",
                name=user_id, role="user", auth_provider="local")
        return _override

    app.dependency_overrides[auth_deps.get_current_user] = actor("modeler-1")
    with TestClient(app) as c:
        yield {"client": c, "actor": actor, "tmp": tmp_path}
    reset_deliverable_repo()


def _independent_parse(obj_text: str):
    """An independent OBJ reader: counts vertices/faces from raw text —
    deliberately NOT modeling_service.validate_asset."""
    verts, faces = [], []
    for line in obj_text.splitlines():
        if line.startswith("v "):
            verts.append(tuple(float(x) for x in line.split()[1:4]))
        elif line.startswith("f "):
            faces.append([int(i) for i in line.split()[1:]])
    assert verts and faces, "independent reader found no geometry"
    return verts, faces


def test_j24_request_download_independent_read_and_edit(client):
    c = client["client"]
    # 1. Request a box model through the REAL API.
    made = c.post("/modeling/assets", json={
        "kind": "box", "size": [2.0, 1.0, 1.0], "name": "plate"})
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["model"]["vertices"] == 8 and body["model"]["faces"] == 6
    artifact_id = body["artifact_id"]
    obj_name = next(f for f in body["files"] if f.endswith(".obj"))

    # 2. Download the ACTUAL bytes through the artifacts API.
    dl = c.get(f"/artifacts/{artifact_id}/files/{obj_name}")
    assert dl.status_code == 200 and dl.content
    verts, faces = _independent_parse(dl.text)
    xs = [v[0] for v in verts]
    assert max(xs) - min(xs) == pytest.approx(2.0), "requested width realized"
    ys = [v[1] for v in verts]
    assert max(ys) - min(ys) == pytest.approx(1.0)

    # 3. Change a dimension → the geometry changes as requested.
    edited = c.post("/modeling/assets", json={
        "kind": "box", "size": [5.0, 1.0, 1.0], "name": "plate"})
    assert edited.status_code == 200
    edited_obj = next(f for f in edited.json()["files"] if f.endswith(".obj"))
    dl2 = c.get(f"/artifacts/{edited.json()['artifact_id']}/files/{edited_obj}")
    verts2, _ = _independent_parse(dl2.text)
    assert max(v[0] for v in verts2) - min(v[0] for v in verts2) == \
        pytest.approx(5.0), "the edit changed the geometry as requested"
    assert edited.json()["model"]["sha256"] != body["model"]["sha256"]

    # 4. Deterministic re-export of the SAME specification: same digest.
    again = c.post("/modeling/assets", json={
        "kind": "box", "size": [2.0, 1.0, 1.0], "name": "plate"})
    assert again.status_code == 200
    assert again.json()["model"]["sha256"] == body["model"]["sha256"]

    # 5. Cross-user download is DENIED (owner-authorized artifacts).
    c2 = TestClient(c.app)
    c2.app.dependency_overrides[auth_deps.get_current_user] = \
        client["actor"]("someone-else")
    other = c2.get(f"/artifacts/{artifact_id}/files/{obj_name}")
    assert other.status_code in (403, 404), other.text

    # §27.1: emit the journey's OBSERVED product receipt (real artifact
    # IDs, digests, the independent reader's geometry measurements).
    from backend.tests.journey_observations import emit
    emit("J24", {
        "artifact_id": artifact_id,
        "artifact_file": obj_name,
        "downloaded_sha256": __import__("hashlib").sha256(
            dl.content).hexdigest(),
        "independent_reader": {"vertices": len(verts), "faces": faces,
                               "width": max(xs) - min(xs),
                               "height": max(ys) - min(ys)},
        "edit_artifact_id": edited.json()["artifact_id"],
        "edit_sha256": edited.json()["model"]["sha256"],
        "edit_width": max(v[0] for v in verts2) - min(v[0] for v in verts2),
        "reproducible_reexport_sha256": again.json()["model"]["sha256"],
        "cross_user_denied_status": other.status_code,
        "observed_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    })


def test_j24_malformed_and_oversized_refused_at_admission(client):
    c = client["client"]
    # Unsupported kind is REFUSED, never silently coerced to a website.
    bad = c.post("/modeling/assets", json={"kind": "deathstar"})
    assert bad.status_code == 422
    # Out-of-range segments refused.
    bad3 = c.post("/modeling/assets", json={"kind": "sphere",
                                            "segments": 999})
    assert bad3.status_code in (422, 400)
    # An oversized mesh is refused at admission (bounded resource caps):
    # a 256-segment sphere exceeds the artifact store's per-file cap.
    big = c.post("/modeling/assets", json={"kind": "sphere",
                                           "segments": 256})
    assert big.status_code == 422, big.text
    assert "exceed" in big.json()["detail"].lower()


def test_j24_publication_records_durable_verified_deliverable(client):
    c = client["client"]
    made = c.post("/modeling/assets", json={
        "kind": "cylinder", "size": [1.0, 1.0, 3.0], "name": "shaft"})
    assert made.status_code == 200
    delivery = made.json().get("delivery") or {}
    # The durable deliverable went through staged publication.
    assert delivery.get("publication_stage") in (
        "published", "completion_emitted"), delivery
    assert delivery.get("verification", {}).get("verdict") == "pass", json.dumps(delivery, default=str)[:800]


def test_j24_modeling_independent_of_website_generation(client):
    """A modeling request must never route through generate_project's
    website fallback — the modeling endpoint's artifact contains ONLY
    the OBJ + model metadata."""
    c = client["client"]
    made = c.post("/modeling/assets", json={
        "kind": "sphere", "segments": 8, "name": "ball"})
    assert made.status_code == 200
    files = made.json()["files"]
    assert any(f.endswith(".obj") for f in files)
    assert not any(f.endswith(".html") for f in files)


@pytest_asyncio.fixture
async def pg_schema():
    """The §17.7 deliverable contract on the DURABLE PostgreSQL profile —
    migration 0023 + PostgresDeliverableRepository through the REAL
    production publication path."""
    import uuid as _uuid
    from backend.tests.test_phase24_feedback_lifecycle_pg import _pg_up
    if not _pg_up():
        pytest.skip("real PostgreSQL required on :55432")
    from backend.repositories.postgres.database import PostgresDatabaseManager
    schema = "p27_mj_" + _uuid.uuid4().hex[:8]
    db = PostgresDatabaseManager(
        "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test",
        schema=schema)
    await db.initialize()
    from backend.repositories.postgres.repositories import (
        PostgresDeliverableRepository)
    set_deliverable_repo(PostgresDeliverableRepository(db))
    yield db
    reset_deliverable_repo()
    await db.close(drop_schema=True)


@pytest.mark.asyncio
async def test_j24_postgres_profile_publication(pg_schema):
    """A model asset published through the REAL postgres deliverable
    contract: candidate → staged publication → typed verification →
    owner-first lookup; a stranger's lookup looks identical to missing
    (§12.28). Regression for the legacy dict-contract refusal that broke
    every file-artifact publication on this profile."""
    from backend.services.artifact_builder import generate_model_asset
    metadata = await generate_model_asset(
        "pg-owner-1", {"kind": "box", "size": [2.0, 1.0, 1.0]},
        name="pg-plate")
    delivery = metadata["delivery"]
    assert delivery.get("publication_stage") in (
        "published", "completion_emitted"), delivery
    assert delivery.get("verification", {}).get("verdict") == "pass", delivery
    repo = pg_schema_repo()
    record = await repo.get_for_owner(delivery["deliverable_id"], "pg-owner-1")
    assert record is not None and record.owner_user_id == "pg-owner-1"
    stranger = await repo.get_for_owner(delivery["deliverable_id"], "stranger")
    assert stranger is None, "a stranger's lookup must look identical to missing"


def pg_schema_repo():
    from backend.repositories.deliverable_repository import get_deliverable_repo
    return get_deliverable_repo()
