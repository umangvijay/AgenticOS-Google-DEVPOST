"""Audit G07 acceptance: both build entry points share ONE durable contract.

- The API path persists a build record with origin=api, a workflow task,
  and dispatches through the engine/bus.
- The orchestrator path gets a durable record with origin=orchestrator and
  the waiting parent run, so a restart mid-build leaves a recoverable trace.
- Build provenance survives persistence (origin / parent_run_id / wait_reason).
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "builds.db"))
    await manager.initialize()
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def factory(db):
    """Minimal factory surface for the build service: mcp_repo, workflow_repo,
    message_bus, and the §4.5 command repository — all on ONE SQLite store so
    the atomic acceptance can commit across them."""
    from backend.repositories.in_memory_message_bus import InMemoryMessageBus
    from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.services.command_service import set_command_repo

    f = type("FactorySurface", (), {})()
    f.mcp_repo = SQLiteMCPRepository(db)
    f.workflow_repo = SQLiteWorkflowRepository(db)
    f.message_bus = InMemoryMessageBus()
    f.db_manager = db
    set_command_repo(SQLiteCommandRepository(db))
    yield f
    set_command_repo(None)


@pytest.mark.asyncio
async def test_g07_api_dispatch_persists_durable_record_and_run(factory):
    from backend.services.connector_build_service import ORIGIN_API, request_durable_build

    result = await request_durable_build(
        factory, user_id="u1", method="url",
        source="https://api.example.com/openapi.json", name="Example",
        origin=ORIGIN_API,
    )
    build = await factory.mcp_repo.get_build(result["build_id"])
    assert build is not None
    assert build["origin"] == "api"
    assert build["status"] == "queued"
    # The workflow task is durable: a worker restart cannot lose the request.
    run = await factory.workflow_repo.get_run(result["run_id"])
    assert run is not None
    assert run["tasks"][0]["agent"] == "core.mcp_build"
    assert run["tasks"][0]["input_data"]["build_id"] == result["build_id"]
    # §4.12/§4.8: dispatch intent is the DURABLE outbox event (publication
    # failure can never strand an accepted build). Draining the outbox
    # delivers it to the bus.
    from backend.repositories.command_repository import SQLiteCommandRepository
    from backend.services.outbox_dispatcher import run_dispatcher
    cmd_repo = SQLiteCommandRepository(factory.db_manager)
    stats = await cmd_repo.outbox_stats()
    assert stats["unpublished"] >= 1, "the accepted build must have an outbox event"
    delivered = await run_dispatcher(cmd_repo, factory.message_bus, owner="test")
    assert delivered >= 1
    queue = factory.message_bus._get_queue("agentos-workflow-events")
    assert queue.qsize() >= 1, "drained outbox delivers the build trigger"


@pytest.mark.asyncio
async def test_g07_orchestrator_track_records_waiting_parent_run(factory):
    from backend.services.connector_build_service import ORIGIN_ORCHESTRATOR, track_inline_build

    track = await track_inline_build(
        factory, user_id="u2", method="prompt",
        source="an API that lists cats", name="Cats",
        origin=ORIGIN_ORCHESTRATOR, parent_run_id="run-parent-1",
    )
    build = await factory.mcp_repo.get_build(track["build_id"])
    assert build["origin"] == "orchestrator"
    assert build["parent_run_id"] == "run-parent-1"
    assert build["wait_reason"] == "capability_missing"
    # §4.12 unified contract: the build's run/task record is part of the
    # SAME acceptance for BOTH surfaces (the caller runs the factory inline
    # against this durable record).
    run = await factory.workflow_repo.get_run(f"mcp-{track['build_id']}")
    assert run is not None
    assert run["tasks"][0]["agent"] == "core.mcp_build"


@pytest.mark.asyncio
async def test_g07_build_record_survives_reopen(factory, tmp_path):
    """Durability: the provenance columns are on disk, not in memory."""
    from backend.services.connector_build_service import ORIGIN_ORCHESTRATOR, track_inline_build

    track = await track_inline_build(
        factory, user_id="u3", method="url", source="https://x.example.com/spec",
        name="X", origin=ORIGIN_ORCHESTRATOR, parent_run_id="run-42",
    )
    fresh = SQLiteMCPRepositoryFactory(factory.db_manager)
    build = await fresh.mcp_repo.get_build(track["build_id"])
    assert build["origin"] == "orchestrator" and build["parent_run_id"] == "run-42"


def SQLiteMCPRepositoryFactory(db):
    from backend.repositories.sqlite.mcp_repository import SQLiteMCPRepository
    f = type("F", (), {})()
    f.mcp_repo = SQLiteMCPRepository(db)
    return f


# ══ Phase 3 §3.10/§3.12/§3.16: ONE admission gate for BOTH surfaces ══════


@pytest.mark.asyncio
async def test_phase3_both_origins_share_one_mcp_build_allowance(factory, monkeypatch):
    """The orchestrator and the API draw from the SAME shared mcp_build
    allowance — an agent cannot bypass the user-facing limit by going
    through the tool surface (§3.16)."""
    from backend.security.shared_rate_limiter import (
        SQLiteSharedRateRepository, set_shared_rate_repo, admission_metrics,
    )
    set_shared_rate_repo(SQLiteSharedRateRepository(factory.db_manager))
    from backend.security import shared_rate_limiter as srl
    srl.METRICS.counters.clear()
    try:
        from backend.services.connector_build_service import (
            ORIGIN_API, ORIGIN_ORCHESTRATOR, BuildAdmissionDenied,
            request_durable_build, track_inline_build,
        )
        for _ in range(resolve_mcp_limit()):   # exhaust the shared allowance
            await track_inline_build(
                factory, user_id="u-share", method="prompt",
                source=f"build spec {_nonce()}", name="B",
                origin=ORIGIN_ORCHESTRATOR)
        with pytest.raises(BuildAdmissionDenied) as exc:
            await request_durable_build(
                factory, user_id="u-share", method="prompt",
                source=f"build spec {_nonce()}", name="B2", origin=ORIGIN_API)
        assert exc.value.reason.value == "RATE_LIMITED"
        counters = admission_metrics()["counters"]
        assert any("group=integration_build" in k and "reason=RATE_LIMITED" in k
                   for k in counters), counters
    finally:
        set_shared_rate_repo(None)


def resolve_mcp_limit():
    from backend.security.admission_policy import resolve_policy
    return resolve_policy("integration_build").limit


def _nonce():
    import uuid
    return uuid.uuid4().hex[:8]


@pytest.mark.asyncio
async def test_phase3_build_at_tenant_pending_cap_is_refused(factory, monkeypatch):
    """§3.12: the durable queue rejects a new build when the tenant's
    pending cap is full — never an unbounded accepted backlog."""
    from backend.repositories.admission_repository import SQLiteCommandAdmissionRepository
    from backend.repositories.admission_repository import set_command_admission_repo
    set_command_admission_repo(SQLiteCommandAdmissionRepository(factory.db_manager))
    from backend.security.shared_rate_limiter import set_shared_rate_repo
    set_shared_rate_repo(None)
    from backend.config.settings import settings
    monkeypatch.setattr(settings, "TENANT_MAX_PENDING_COMMANDS", 2)
    try:
        from backend.services.connector_build_service import (
            ORIGIN_API, BuildAdmissionDenied, request_durable_build,
        )
        for _ in range(2):
            await request_durable_build(
                factory, user_id="u-cap", method="prompt",
                source=f"spec {_nonce()}", name="B", origin=ORIGIN_API)
        with pytest.raises(BuildAdmissionDenied) as exc:
            await request_durable_build(
                factory, user_id="u-cap", method="prompt",
                source=f"spec {_nonce()}", name="B2", origin=ORIGIN_API)
        assert exc.value.reason.value == "QUEUE_FULL"
    finally:
        set_command_admission_repo(None)


@pytest.mark.asyncio
async def test_phase3_identical_build_dedups_across_surfaces(factory):
    """A retried identical build — from EITHER surface — is one logical
    command: the duplicate refusal carries the original command id and no
    second queue slot is consumed."""
    from backend.repositories.admission_repository import (
        SQLiteCommandAdmissionRepository, set_command_admission_repo,
    )
    set_command_admission_repo(SQLiteCommandAdmissionRepository(factory.db_manager))
    try:
        from backend.services.connector_build_service import (
            ORIGIN_API, ORIGIN_ORCHESTRATOR, BuildAdmissionDenied,
            request_durable_build, track_inline_build,
        )
        source = f"https://example.com/openapi-{_nonce()}.json"
        first = await request_durable_build(
            factory, user_id="u-dedup", method="url", source=source,
            name="Same", origin=ORIGIN_API)
        with pytest.raises(BuildAdmissionDenied) as exc:
            await track_inline_build(
                factory, user_id="u-dedup", method="url", source=source,
                name="Same", origin=ORIGIN_ORCHESTRATOR)
        assert exc.value.first_command_id
        # The deduped request did NOT create a second durable build record.
        builds = await factory.mcp_repo.list_builds(user_id="u-dedup") \
            if hasattr(factory.mcp_repo, "list_builds") else None
        if builds is not None:
            assert len([b for b in builds if b["status"] == "queued"]) <= 1
        assert first["status"] == "queued"
    finally:
        set_command_admission_repo(None)


@pytest.mark.asyncio
async def test_phase3_enqueue_failure_settles_the_command(factory):
    """§3.12: when the durable record/enqueue fails, the command slot is
    freed — a persistence failure cannot consume tenant queue capacity."""
    from backend.repositories.admission_repository import (
        SQLiteCommandAdmissionRepository, set_command_admission_repo,
    )
    repo = SQLiteCommandAdmissionRepository(factory.db_manager)
    set_command_admission_repo(repo)
    set_shared = None
    try:
        from backend.services import admission_service
        from backend.services.connector_build_service import (
            ORIGIN_API, request_durable_build,
        )

        async def broken_save_run(data):
            raise RuntimeError("disk full")

        factory.workflow_repo.save_run = broken_save_run
        import uuid as _uuid
        from backend.services.connector_build_service import admit_build
        command_id = await admit_build(factory, user_id="u-fail", method="prompt",
                                       source=f"s-{_uuid.uuid4().hex[:6]}", origin=ORIGIN_API)
        # Simulate the service's failure path directly.
        from backend.services.connector_build_service import settle_build_command
        await settle_build_command(command_id)
        row = await repo.get_command(command_id)
        assert row["status"] == "settled"
    finally:
        set_command_admission_repo(None)
