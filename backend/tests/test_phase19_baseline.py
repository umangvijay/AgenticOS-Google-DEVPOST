"""Phase 19 §19.0 — the carryover baseline: C1–C10 REOPENED with
reproducing probes. Each finding carries the probe that failed, and
stays open until the probe passes against the production path."""
import pytest


def test_c1_firestore_factory_scope_missing_import():
    """C1 probe: the Firestore factory section calls set_deliverable_repo
    without importing it in that scope — initializing with
    STORAGE_BACKEND=firestore must not raise NameError."""
    import ast
    src = open("backend/repositories/factory.py").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                "_init_firestore" in node.name:
            names = set()
            for n in ast.walk(node):
                if isinstance(n, ast.ImportFrom):
                    for a in n.names:
                        names.add(a.asname or a.name)
                elif isinstance(n, ast.Import):
                    for a in n.names:
                        names.add((a.asname or a.name).split(".")[0])
            assert "set_deliverable_repo" in names, \
                "the Firestore init scope must import set_deliverable_repo"


def test_c2_multi_file_digest_mismatch_probe():
    """C2 probe: the verifier hashes the CONCATENATION of stored bytes
    against per-file manifest digests — a valid two-file artifact fails
    and a one-file subset passes."""
    import asyncio, tempfile, os
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.models.deliverable import Deliverable
    from backend.services.artifact_verifier import verify_artifact

    async def main():
        db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "c2.db"))
        await db.initialize()
        repo = DeliverableRepository(db)
        file_a = b"alpha content"
        file_b = b"beta content"
        import hashlib as _hl
        d = await repo.create_candidate(Deliverable(
            deliverable_id="x", deliverable_key="k", revision=1,
            tenant_id="t", owner_user_id="u",
            deliverable_type="source_project",
            file_manifest=[
                {"path": "a.txt", "content": file_a.decode(),
                 "sha256": _hl.sha256(file_a).hexdigest(),
                 "byte_length": len(file_a)},
                {"path": "b.txt", "content": file_b.decode(),
                 "sha256": _hl.sha256(file_b).hexdigest(),
                 "byte_length": len(file_b)}]),
            deliverable_key="k")
        await repo.transition(d.deliverable_id, "stored",
                              file_manifest=d.file_manifest,
                              content_checksum="manifest-digest")
        # §19.2: the storage resolver returns the PER-FILE map.
        async def read_files(aid):
            return {"a.txt": file_a, "b.txt": file_b}
        out = await verify_artifact(
            artifact_id=d.deliverable_id, owner_user_id="u", repo=repo,
            stored_bytes_fn=read_files)
        assert out.verdict == "pass", \
            f"C2: both files verified → pass (got {out.verdict}: " \
            f"{out.blocking_reasons})"
        # A ONE-FILE SUBSET must fail (the reviewer's second probe).
        async def read_subset(aid):
            return {"a.txt": file_a}   # b.txt missing
        out2 = await verify_artifact(
            artifact_id=d.deliverable_id, owner_user_id="u", repo=repo,
            stored_bytes_fn=read_subset)
        assert out2.verdict == "fail", \
            "C2: a subset must fail — another file is missing"
        # An EMPTY manifest cannot pass.
        d2 = await repo.create_candidate(Deliverable(
            deliverable_id="y", deliverable_key="k2", revision=1,
            tenant_id="t", owner_user_id="u",
            deliverable_type="source_project",
            file_manifest=[]),
            deliverable_key="k2")
        await repo.transition(d2.deliverable_id, "stored",
                              file_manifest=[],
                              content_checksum="x")
        out3 = await verify_artifact(
            artifact_id=d2.deliverable_id, owner_user_id="u", repo=repo,
            stored_bytes_fn=read_files)
        assert out3.verdict == "fail", \
            "C2: an empty manifest is rejected"
        await db.close()
        return "pass"
    verdict = asyncio.run(main())
    assert verdict == "pass"


def test_c3_emissionless_completion_probe():
    """C3 probe: publish_deliverable WITHOUT an emit_fn still advances to
    completion_emitted (the default path), reporting more than happened."""
    import asyncio, tempfile, os
    from backend.repositories.deliverable_repository import (
        DeliverableRepository)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.models.deliverable import Deliverable
    from backend.services.deliverable_publisher import publish_deliverable

    async def main():
        db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "c3.db"))
        await db.initialize()
        repo = DeliverableRepository(db)
        d = await repo.create_candidate(Deliverable(
            deliverable_id="x", deliverable_key="k", revision=1,
            tenant_id="t", owner_user_id="u", deliverable_type="resume_html",
            file_manifest=[{"path": "r.html", "content": "<p>x</p>"}]),
            deliverable_key="k")
        tx = await publish_deliverable(
            repo=repo, deliverable_id=d.deliverable_id,
            files=[{"path": "r.html", "content": "<p>x</p>"}],
            store_fn=None, verify_stored_fn=None, emit_fn=None)
        print("C3 stage without emit:", tx.stage)
        await db.close()
        return tx.stage
    stage = asyncio.run(main())
    assert stage != "completion_emitted", \
        "C3 reopened: completion reported without an emission callback"


def test_c4_confirm_bypasses_expected_revision_probe():
    """C4 probe: the confirm action transitions without the atomic
    expected-revision check that remove/edit run."""
    src = open("backend/api/routers/deliverables.py").read()
    # The confirm branch must run the same revision gate.
    confirm_idx = src.index('"confirm"')
    section = src[confirm_idx:confirm_idx + 2000]
    assert "review_with_revision_check" in section or \
        "expected_revision" in section, \
        "C4 reopened: confirm bypasses the expected-revision gate"


def test_c5_sync_metering_dispatches_before_reservation():
    """C5 probe: the sync metering path calls the provider FIRST and only
    then schedules accounting — REPAIRED: reservation now precedes
    dispatch (§29.6); the probe asserts the repaired order."""
    import inspect
    from backend.services import metered_model as mm
    src = inspect.getsource(mm)
    gen = src.index("def generate_content(self")
    gen_body = src[gen:src.index("def _fresh_manager")]
    # §29.6 repair of this probe: the C5 defect (dispatch before the
    # reservation) is FIXED — the governed reservation lifecycle now
    # precedes the provider call in the sync path. The probe asserts the
    # repaired order; dispatch-before-reservation is the regression.
    reservation = gen_body.index("self._reserve_sync(")
    provider_call = gen_body.index("self._inner.generate_content")
    assert reservation < provider_call, \
        "C5 reopened: the sync path dispatches before the reservation"


def test_c7_no_production_generation_callers():
    """C7 probe: the auth-generation helpers have no production callers."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "nextAuthGeneration\\|isCurrentGeneration",
         "frontend/src", "--include=*.ts", "--include=*.tsx"],
        capture_output=True, text=True)
    lines = [l for l in out.stdout.splitlines()
             if "auth-state.ts" not in l and "test" not in l]
    assert lines, "C7 reopened: no production callers for the generation guard"


def test_c8_run_completed_substring_probe():
    """C8 probe: a TASK event whose type contains COMPLETED marks the
    whole run complete in useRunEvents."""
    import inspect
    src = open("frontend/src/hooks/useRunEvents.ts").read()
    assert 'event.type?.includes("COMPLETED")' not in src, \
        "C8 reopened: substring completion matching is still present"


@pytest.mark.asyncio
async def test_c9_empty_submission_identity_saved(tmp_path):
    """A retry with the restored draft key resolves to the original run.

    The mounted draft and production browser tests exercise localStorage and
    the outgoing request. This side exercises durable command acceptance.
    """
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.command_service import accept_command, set_command_repo

    db = DatabaseManager(str(tmp_path / "draft-retry.db"))
    await db.initialize()
    try:
        set_command_repo(SQLiteCommandRepository(db))
        request = dict(command_type="workflow", owner_user_id="draft-owner",
                       idempotency_key="sub-restored-draft", request_digest="same-request")
        first = await accept_command(**request, run_id="original-run")
        retry = await accept_command(**request, run_id="duplicate-run")
        assert first.accepted and retry.accepted and retry.deduplicated
        assert retry.command_id == first.command_id
        assert retry.run_id == "original-run"
    finally:
        set_command_repo(None)
        await db.close()
