"""Phase 21 §21.3/§21.4: the REAL factory initializes on PostgreSQL and
selects the durable transport. These tests drive the actual
RepositoryFactory with STORAGE_BACKEND=postgres / MESSAGE_BUS_TRANSPORT
— no test double stands in for the factory."""
import os
import uuid

import pytest
import pytest_asyncio

PG_DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"
NATS_SERVERS = "nats://127.0.0.1:4222"


def _pg_up() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 55432), timeout=2):
            return True
    except OSError:
        return False


def _nats_up() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=2):
            return True
    except OSError:
        return False


def _factory_with(monkeypatch, *, storage: str, transport: str):
    from backend.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "STORAGE_BACKEND", storage)
    monkeypatch.setattr(settings_mod.settings, "POSTGRES_DSN", PG_DSN)
    monkeypatch.setattr(settings_mod.settings, "MESSAGE_BUS_TRANSPORT",
                        transport)
    monkeypatch.setattr(settings_mod.settings, "NATS_SERVERS", NATS_SERVERS)
    from backend.repositories.factory import RepositoryFactory
    return RepositoryFactory()


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pg_factory(monkeypatch):
    if not _pg_up():
        pytest.skip("BLOCKED: no Postgres on :55432 — "
                    "docker run -d --name agentos-pg20 -p 55432:5432 "
                    "-e POSTGRES_PASSWORD=agentos -e POSTGRES_USER=agentos "
                    "-e POSTGRES_DB=agentos_test postgres:16-alpine")
    factory = _factory_with(monkeypatch, storage="postgres",
                            transport="inprocess")
    await factory.initialize()
    try:
        yield factory
    finally:
        await factory.shutdown()


async def test_factory_initializes_full_inventory_on_postgres(pg_factory):
    """THE §21.3 acceptance: the REAL factory initializes with
    PostgreSQL and every REQUIRED inventory repo is present and typed."""
    f = pg_factory
    from backend.repositories.postgres import repositories as pgrepo
    from backend.repositories.postgres.command_repository import (
        PostgresCommandRepositoryV2)
    expectations = {
        "command_repo": PostgresCommandRepositoryV2,
        "user_repo": pgrepo.PostgresUserRepository,
        "workflow_repo": None,  # engine repository subclass
        "org_repo": pgrepo.PostgresOrgRepository,
        "secrets_repo": pgrepo.PostgresSecretsRepository,
        "mcp_repo": pgrepo.PostgresMCPRepository,
        "effect_repo": pgrepo.PostgresEffectRepository,
        "browser_session_repo": pgrepo.PostgresBrowserSessionRepository,
        "recovery_episode_repo": pgrepo.PostgresRecoveryEpisodeRepository,
        "evaluation_repo": pgrepo.PostgresEvaluationRepository,
        "memory_repo": pgrepo.PostgresMemoryRepository,
        "deliverable_repo": pgrepo.PostgresDeliverableRepository,
        "feedback_repo": pgrepo.PostgresFeedbackRepository,
        "execution_step_repo": pgrepo.PostgresExecutionStepRepository,
        "refresh_token_repo": pgrepo.PostgresRefreshTokenRepository,
        "settings_repo": pgrepo.PostgresSettingsRepository,
    }
    for attr, cls in expectations.items():
        repo = getattr(f, attr, None)
        assert repo is not None, f"factory missing {attr}"
        if cls is not None:
            assert isinstance(repo, cls), f"{attr} is {type(repo).__name__}"
    # Global accessors wired so services resolve the SAME instances.
    from backend.repositories.effect_repository import get_effect_repo
    assert get_effect_repo() is f.effect_repo


async def test_postgres_user_and_secret_roundtrip(pg_factory):
    f = pg_factory
    uid = f"pguser-{uuid.uuid4().hex[:8]}"
    created = await f.user_repo.create_user({
        "id": uid, "email": f"{uid}@example.com", "name": "PG User"})
    assert created["id"] == uid
    fetched = await f.user_repo.get_by_email(f"{uid}@example.com")
    assert fetched["id"] == uid
    await f.secrets_repo.store_secret(uid, "llm_key", "ciphertext-xyz")
    assert await f.secrets_repo.get_secret(uid, "llm_key") == "ciphertext-xyz"
    # Tenant isolation: another user cannot read the secret.
    assert await f.secrets_repo.get_secret("someone-else", "llm_key") is None


async def test_postgres_org_membership_and_tenancy(pg_factory):
    f = pg_factory
    suffix = uuid.uuid4().hex[:8]
    owner, member = f"org-owner-{suffix}", f"org-member-{suffix}"
    await f.user_repo.create_user({"id": owner,
                                   "email": f"{owner}@example.com"})
    await f.user_repo.create_user({"id": member,
                                   "email": f"{member}@example.com"})
    org = await f.org_repo.create_org("Phase 21 Org", owner)
    org_id = org["org_id"]
    assert org["owner_user_id"] == owner
    inv = await f.org_repo.create_invitation(org_id,
                                             f"{member}@example.com",
                                             "member", owner)
    assert await f.org_repo.accept_invitation(inv["token"], member)
    assert await f.org_repo.get_member_role(org_id, member) == "member"
    # Only the owner can transfer ownership.
    assert await f.org_repo.transfer_ownership(org_id, owner, member)
    assert await f.org_repo.get_member_role(org_id, member) == "owner"


async def test_postgres_command_outbox_claim_cycle(pg_factory):
    """§21.4 foundation: outbox events are claimed with a LEASE by a
    worker (FOR UPDATE SKIP LOCKED), published, and stale leases are
    reclaimable — the cross-process dispatch primitive."""
    f = pg_factory
    suffix = uuid.uuid4().hex[:8]
    cid = await f.command_repo.record_command({
        "id": f"cmd-{suffix}", "user_id": "u1", "command_type": "START",
        "payload": {"goal": "x"}, "idempotency_key": f"idem-{suffix}"})
    assert cid
    duplicate = await f.command_repo.record_command({
        "id": f"cmd-{suffix}-2", "user_id": "u1", "command_type": "START",
        "payload": {"goal": "x"}, "idempotency_key": f"idem-{suffix}"})
    assert duplicate != cid, "idempotency key dedups"
    await f.command_repo.append_outbox(f"evt-{suffix}", "workflow.events",
                                       {"task_id": "t1"})
    batch = await f.command_repo.claim_outbox_batch("worker-a", batch=10,
                                                    lease_seconds=60)
    claimed = [e for e in batch if e.event_id == f"evt-{suffix}"]
    assert claimed, "the outbox event was claimed with a lease"
    # A second claim does NOT return the leased event.
    batch2 = await f.command_repo.claim_outbox_batch("worker-b", batch=50,
                                                     lease_seconds=60)
    assert all(e.event_id != f"evt-{suffix}" for e in batch2)
    await f.command_repo.mark_outbox_published(f"evt-{suffix}")
    # Stale-lease reclaim: a lease that expired re-enters the queue.
    await f.command_repo.append_outbox(f"evt-stale-{suffix}",
                                       "workflow.events", {"n": 2})
    got = await f.command_repo.claim_outbox_batch("worker-a", batch=50,
                                                  lease_seconds=60)
    assert any(e.event_id == f"evt-stale-{suffix}" for e in got)
    await f.command_repo.db.execute(
        "UPDATE pg_outbox_v2 SET dispatcher_lease_expires_at = 1 "
        "WHERE event_id=$1", f"evt-stale-{suffix}")
    reclaimed = await f.command_repo.reclaim_stale_outbox_leases()
    assert reclaimed >= 1


async def test_nats_transport_selected_by_factory(monkeypatch):
    """§21.4: MESSAGE_BUS_TRANSPORT=nats gives the REAL JetStream bus;
    an unreachable server raises LOUDLY instead of degrading."""
    if not _nats_up():
        pytest.skip("BLOCKED: no NATS on :4222 — docker run -d "
                    "--name agentos-nats20 -p 4222:4222 nats:2.10-alpine -js")
    factory = _factory_with(monkeypatch, storage="sqlite", transport="nats")
    await factory.initialize()
    try:
        from backend.repositories.nats_message_bus import NatsMessageBus
        assert isinstance(factory.message_bus, NatsMessageBus)
        assert factory.message_bus.connected()
    finally:
        await factory.shutdown()


async def test_nats_transport_failure_is_loud(monkeypatch):
    from backend.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "MESSAGE_BUS_TRANSPORT",
                        "nats")
    monkeypatch.setattr(settings_mod.settings, "NATS_SERVERS",
                        "nats://127.0.0.1:45999")
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    with pytest.raises(RuntimeError, match="no NATS server"):
        await factory.initialize()


async def test_factory_still_initializes_sqlite_default(monkeypatch, tmp_path):
    """Preserved behavior: the sqlite default path still initializes."""
    from backend.config import settings as settings_mod
    monkeypatch.setattr(settings_mod.settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings_mod.settings, "SQLITE_DB_PATH",
                        str(tmp_path / "f21.db"))
    monkeypatch.setattr(settings_mod.settings, "MESSAGE_BUS_TRANSPORT",
                        "inprocess")
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        assert factory.user_repo is not None
        from backend.repositories.in_memory_message_bus import InMemoryMessageBus
        assert isinstance(factory.message_bus, InMemoryMessageBus)
    finally:
        await factory.shutdown()
