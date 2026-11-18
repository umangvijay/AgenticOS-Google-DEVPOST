"""Phase 29 — credential binding lifecycle (§29.5).

Every stored credential carries a durable binding bound to its owner.
Re-storing is a ROTATION (version + epoch advance → the binding id
changes → validation evidence under the previous context stops being
current); deletion REVOKES the binding. Another owner's binding is
independent — installing or sharing a tool never shares a credential.
Raw secret values never enter binding rows.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.services.credential_binding_store import CredentialBindingStore


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "credential-bindings.db"))
    await manager.initialize()
    try:
        yield manager
    finally:
        await manager.close()


@pytest_asyncio.fixture
async def store(db):
    return CredentialBindingStore(db)


@pytest.mark.asyncio()
async def test_first_store_creates_binding_v1_e1(store):
    result = await store.record_store(owner_id="u1", name="gemini")
    assert result["rotated"] is False
    assert result["secret_version"] == "v1"
    assert result["authorization_epoch"] == "e1"
    binding = await store.get_binding("u1", "gemini")
    assert binding["status"] == "active"


@pytest.mark.asyncio()
async def test_restore_is_a_rotation_that_changes_the_binding_id(store):
    first = await store.record_store(owner_id="u1", name="gemini")
    second = await store.record_store(owner_id="u1", name="gemini")
    assert second["rotated"] is True
    assert second["binding_id"] != first["binding_id"]
    assert second["secret_version"] == "v2"
    assert second["authorization_epoch"] == "e2"
    # Stale evidence under the FIRST context is no longer current.
    assert await store.is_current("u1", "gemini", first["binding_id"]) is False
    assert await store.is_current("u1", "gemini", second["binding_id"]) is True


@pytest.mark.asyncio()
async def test_owners_have_independent_bindings(store):
    """The digest identifies the CONTEXT (ref|version|epoch); ownership
    is enforced by the store's (owner, reference) primary key and the
    per-owner secrets repository — one owner's rotation can never touch
    another owner's binding."""
    a = await store.record_store(owner_id="user-a", name="stripe")
    b = await store.record_store(owner_id="user-b", name="stripe")
    # Both contexts start at v1/e1 — but they are SEPARATE rows.
    assert b["secret_version"] == "v1"
    # User A's rotation does not touch user B's row.
    a2 = await store.record_store(owner_id="user-a", name="stripe")
    assert a2["rotated"] is True
    assert a2["secret_version"] == "v2"
    assert (await store.get_binding("user-b", "stripe"))["secret_version"] == "v1"
    # Revoking A leaves B active.
    await store.revoke("user-a", "stripe")
    assert (await store.get_binding("user-b", "stripe"))["status"] == "active"


@pytest.mark.asyncio()
async def test_delete_revokes_the_binding(store):
    created = await store.record_store(owner_id="u1", name="smtp")
    revoked = await store.revoke("u1", "smtp")
    assert revoked is True
    binding = await store.get_binding("u1", "smtp")
    assert binding["status"] == "revoked"
    # Revoked binding: nothing is current.
    assert await store.is_current("u1", "smtp", created["binding_id"]) is False
    # Re-store after revocation starts a FRESH active binding.
    fresh = await store.record_store(owner_id="u1", name="smtp")
    assert fresh["status"] == "active"


@pytest.mark.asyncio()
async def test_binding_rows_never_hold_secret_values(store):
    await store.record_store(owner_id="u1", name="gemini")
    binding = await store.get_binding("u1", "gemini")
    dumped = json.dumps(binding)
    # The store only sees the reference and lifecycle fields.
    assert "api_key" not in dumped
    assert binding.get("secret_reference") == "cred:gemini"


@pytest.mark.asyncio()
async def test_tool_validation_binding_id_agrees_with_the_store(store):
    """§29.5: evidence computed by the tool-validation context joins the
    durable store by construction — the same reference/version/epoch
    triple derives the same digest in both places."""
    from backend.services.tool_validation import credential_binding_id_from
    created = await store.record_store(owner_id="u1", name="fluentedge")
    binding = await store.get_binding("u1", "fluentedge")
    validation_id = credential_binding_id_from({
        "type": "API_KEY",
        "credential_ref": binding["secret_reference"],
        "secret_version": binding["secret_version"],
        "authorization_epoch": binding["authorization_epoch"],
    })
    assert validation_id == binding["binding_id"], \
        "the tool-validation context and the binding store must agree"
    assert created["binding_id"] == CredentialBindingStore.binding_id_for(
        binding["secret_reference"], binding["secret_version"],
        binding["authorization_epoch"])


@pytest.mark.asyncio()
async def test_credentials_router_records_and_revokes_bindings(tmp_path, monkeypatch):
    """The HTTP path records the binding on store and revokes it on
    delete — the lifecycle is wired at the real entry point."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.factory import RepositoryFactory
    manager = DatabaseManager(str(tmp_path / "cred-router.db"))
    await manager.initialize()
    factory = RepositoryFactory.__new__(RepositoryFactory)
    factory.db_manager = manager
    from backend.repositories.sqlite.secrets_repository import (
        SQLiteSecretsRepository)
    factory.secrets_repo = SQLiteSecretsRepository(manager)

    class _Audit:
        def __init__(self):
            self.events = []

        async def log_event(self, event):
            self.events.append(event)

    factory.audit_repo = _Audit()

    from backend.api.routers import credentials as cred_router
    from backend.services.credential_binding_store import (
        CredentialBindingStore)
    store = CredentialBindingStore(manager)

    from backend.security.secrets_vault import secrets_vault
    secrets_vault.initialize()  # ephemeral dev key outside production

    class _Req:
        app = type("App", (), {"state": type("S", (), {"factory": factory})()})()

    class _User:
        user_id = "router-user"

        def is_admin(self):
            return False

    # A NON-LLM name: LLM provider names trigger live key-validation
    # HTTP calls, which no unit test may perform (§29.14 retrieval
    # boundaries). The lifecycle is identical for every name.
    body = cred_router.StoreCredentialRequest(
        name="fluentedge", values={"api_key": "test-key-do-not-log-xyz"})
    result = await cred_router.store_credential(body, _Req(), _User())
    assert result["stored"] is True
    assert result["binding"]["rotated"] is False
    binding = await store.get_binding("router-user", "fluentedge")
    assert binding and binding["status"] == "active"

    # Rotation through the same entry point.
    result2 = await cred_router.store_credential(body, _Req(), _User())
    assert result2["binding"]["rotated"] is True

    # Deletion revokes.
    await cred_router.delete_credential("fluentedge", _Req(), _User())
    binding = await store.get_binding("router-user", "fluentedge")
    assert binding["status"] == "revoked"
    await manager.close()


def test_binding_digest_function_is_stable():
    """The digest is a pure function of reference|version|epoch — the
    same triple always yields the same binding id."""
    a = CredentialBindingStore.binding_id_for("cred:gemini", "v1", "e1")
    b = CredentialBindingStore.binding_id_for("cred:gemini", "v1", "e1")
    c = CredentialBindingStore.binding_id_for("cred:gemini", "v2", "e1")
    assert a == b and a != c
