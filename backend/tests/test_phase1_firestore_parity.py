"""Phase 1 Firestore contract tests (§1.18: "SQLite and Firestore both pass
the applicable contract tests").

Runs the org/invitation/membership/ownership contract against the REAL
Firestore implementation (backend/repositories/firestore/sidecar.py) whenever
a Firestore emulator is configured:

    FIRESTORE_EMULATOR_HOST=localhost:8080 GOOGLE_CLOUD_PROJECT=test-project \
        pytest backend/tests/test_phase1_firestore_parity.py

Covers the same acceptance matrix as the SQLite suites (§1.15/§1.16):
pending-uniqueness, acceptance validation, revoke, resend semantics, and the
Phase 1.8 ownership-transfer invariants.
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

_EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()

pytestmark = pytest.mark.skipif(
    not _EMULATOR,
    reason="Firestore org parity requires FIRESTORE_EMULATOR_HOST (e.g. localhost:8080)",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_emulator_data():
    """The emulator (like production Firestore) is persistent across runs.
    Wipe the application collections before each test so every test starts
    clean — the equivalent of the SQLite suites' fresh tmp_path database."""
    from backend.repositories.firestore import database as fs_database
    fs_database.FirestoreDB._instance = None
    try:
        client = await fs_database.FirestoreDB.get_client()
        for name in (
            "users", "orgs", "org_members", "org_invitations",
            "org_invitations_pending", "audit_logs",
            "budget_reservations", "budget_reservations_by_key",
            "budget_buckets", "budget_events", "usage_ledger",
        ):
            async for doc in client.collection(name).stream():
                await doc.reference.delete()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None
    yield


@pytest_asyncio.fixture
async def org_repo():
    from backend.config.settings import settings as app_settings
    if not app_settings.GOOGLE_CLOUD_PROJECT:
        pytest.skip("Firestore parity requires GOOGLE_CLOUD_PROJECT (any value against the emulator)")
    from backend.repositories.firestore import database as fs_database
    from backend.repositories.firestore.sidecar import FirestoreOrgRepository
    # pytest-asyncio gives each test its own event loop; the cached
    # AsyncClient's gRPC transport is bound to the loop that created it, so
    # every test gets a fresh client (production uses one loop per process).
    fs_database.FirestoreDB._instance = None
    try:
        yield FirestoreOrgRepository()
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None


def _token(seed: str) -> str:
    return seed + uuid.uuid4().hex[:12]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _expires(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ── org + membership ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_org_makes_the_creator_owner(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Parity Org", owner_id)
    assert await org_repo.get_member_role(org["org_id"], owner_id) == "owner"
    orgs = await org_repo.list_orgs_for_user(owner_id)
    assert any(o["org_id"] == org["org_id"] and o["role"] == "owner" for o in orgs)


@pytest.mark.asyncio
async def test_owner_row_cannot_be_removed_or_demoted(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Protected Org", owner_id)
    assert await org_repo.remove_member(org["org_id"], owner_id) is False
    assert await org_repo.set_member_role(org["org_id"], owner_id, "viewer") is False
    assert await org_repo.get_member_role(org["org_id"], owner_id) == "owner"


# ── invitation lifecycle (§1.5/§1.6/§1.7) ─────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_pending_invitation_is_refused(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Unique Org", owner_id)
    email = "invitee-parity@example.com"
    first = await org_repo.create_invitation(
        org["org_id"], email, "member", owner_id, _hash("tok-a"), _expires())
    assert first, "first invitation must be created"
    second = await org_repo.create_invitation(
        org["org_id"], email, "admin", owner_id, _hash("tok-b"), _expires())
    assert second == {}, "duplicate pending invitation must be refused"


@pytest.mark.asyncio
async def test_acceptance_flow_and_recipient_binding(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Accept Org", owner_id)
    email = "acceptee@example.com"
    token = "accept-" + uuid.uuid4().hex
    invite = await org_repo.create_invitation(
        org["org_id"], email, "member", owner_id, _hash(token), _expires())
    assert invite

    # Wrong recipient (different email case content) is denied.
    with pytest.raises(ValueError, match="different email"):
        await org_repo.accept_invitation(_hash(token), uuid.uuid4().hex, "other@example.com")
    assert await org_repo.get_member_role(org["org_id"], "someone") is None

    # Correct recipient accepts: membership created, invitation accepted.
    acceptee = uuid.uuid4().hex
    result = await org_repo.accept_invitation(_hash(token), acceptee, email)
    assert result["org_id"] == org["org_id"] and result["role"] == "member"
    assert await org_repo.get_member_role(org["org_id"], acceptee) == "member"

    # Second acceptance of the same token is refused.
    with pytest.raises(ValueError):
        await org_repo.accept_invitation(_hash(token), uuid.uuid4().hex, email)

    # The pending-claim is released: a NEW invitation may be issued.
    again = await org_repo.create_invitation(
        org["org_id"], email, "viewer", owner_id, _hash("tok-c"), _expires())
    assert again, "uniqueness claim must be released after acceptance"


@pytest.mark.asyncio
async def test_expired_invitation_cannot_grant_membership(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Expiry Org", owner_id)
    email = "expired@example.com"
    token = "expired-" + uuid.uuid4().hex
    await org_repo.create_invitation(
        org["org_id"], email, "member", owner_id, _hash(token), _expires(days=-1))
    acceptee = uuid.uuid4().hex
    with pytest.raises(ValueError, match="expired"):
        await org_repo.accept_invitation(_hash(token), acceptee, email)
    assert await org_repo.get_member_role(org["org_id"], acceptee) is None


@pytest.mark.asyncio
async def test_revocation_wins_over_later_acceptance(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Revoke Org", owner_id)
    email = "revoked@example.com"
    token = "revoke-" + uuid.uuid4().hex
    invite = await org_repo.create_invitation(
        org["org_id"], email, "member", owner_id, _hash(token), _expires())
    assert await org_repo.revoke_invitation(org["org_id"], invite["invitation_id"]) is True
    acceptee = uuid.uuid4().hex
    with pytest.raises(ValueError, match="already used"):
        await org_repo.accept_invitation(_hash(token), acceptee, email)
    assert await org_repo.get_member_role(org["org_id"], acceptee) is None
    # A new invitation after revocation is allowed (claim released).
    again = await org_repo.create_invitation(
        org["org_id"], email, "member", owner_id, _hash("tok-d"), _expires())
    assert again


@pytest.mark.asyncio
async def test_resend_rotates_the_token_and_preserves_the_role(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Resend Org", owner_id)
    email = "resend@example.com"
    token = "resend-" + uuid.uuid4().hex
    old_hash = _hash(token)
    invite = await org_repo.create_invitation(
        org["org_id"], email, "admin", owner_id, old_hash, _expires())
    new_hash = _hash("rotated-" + uuid.uuid4().hex)
    updated = await org_repo.resend_invitation(
        org["org_id"], invite["invitation_id"], owner_id, new_hash, _expires())
    assert updated and updated["role"] == "admin", "resend must preserve the role"
    # The old token is dead; the rotated token works.
    with pytest.raises(ValueError):
        await org_repo.accept_invitation(old_hash, uuid.uuid4().hex, email)
    acceptee = uuid.uuid4().hex
    result = await org_repo.accept_invitation(new_hash, acceptee, email)
    assert result["role"] == "admin"


# ── Phase 1.8 ownership transfer ──────────────────────────────────────


@pytest.mark.asyncio
async def test_transfer_moves_ownership_atomically(org_repo):
    owner_id = uuid.uuid4().hex
    admin_id = uuid.uuid4().hex
    org = await org_repo.create_org("Transfer Org", owner_id)
    assert await org_repo.add_member(org["org_id"], admin_id, "admin", owner_id)
    result = await org_repo.transfer_ownership(org["org_id"], owner_id, admin_id)
    assert result["new_owner"] == admin_id
    stored = await org_repo.get_org(org["org_id"])
    assert stored["owner_id"] == admin_id
    assert await org_repo.get_member_role(org["org_id"], admin_id) == "owner"
    assert await org_repo.get_member_role(org["org_id"], owner_id) == "admin"


@pytest.mark.asyncio
async def test_transfer_requires_the_current_owner_and_a_member(org_repo):
    owner_id = uuid.uuid4().hex
    member_id = uuid.uuid4().hex
    outsider = uuid.uuid4().hex
    org = await org_repo.create_org("Guarded Org", owner_id)
    with pytest.raises(ValueError, match="current owner"):
        await org_repo.transfer_ownership(org["org_id"], outsider, member_id)
    with pytest.raises(ValueError, match="current member"):
        await org_repo.transfer_ownership(org["org_id"], owner_id, outsider)
    # The failed transfer created no membership for the outsider.
    assert await org_repo.get_member_role(org["org_id"], outsider) is None


@pytest.mark.asyncio
async def test_transfer_to_self_denied(org_repo):
    owner_id = uuid.uuid4().hex
    org = await org_repo.create_org("Self Org", owner_id)
    with pytest.raises(ValueError, match="themselves"):
        await org_repo.transfer_ownership(org["org_id"], owner_id, owner_id)
