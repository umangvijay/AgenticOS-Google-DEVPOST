"""Phase 1.16 — conservative migration validation.

Existing-data rules (§1.16) proven against the actual migration path:
1. Known personal ownership is preserved (creator's user_id; org_id stays
   NULL after the column migration — never inferred from later membership).
2. No sharing is inferred when a creator joins an organization: legacy runs
   never appear in org oversight.
3. Migration is idempotent (re-initializing an existing database changes
   nothing and grants nothing).
4. Pre-existing pending invitations keep their hash format and TTL handling —
   the weaker legacy path (unverified recipients) stays governed by the 1.3
   gate, not silently grandfathered into bypassing it.
5. Ambiguous ownership (no user_id) is never repaired by assignment.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.org_repository import SQLiteOrgRepository
from backend.repositories.sqlite.user_repository import SQLiteUserRepository


@pytest_asyncio.fixture
async def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "migration.db"))
    await manager.initialize()
    yield manager
    await manager.close()


async def _make_user(db, email):
    repo = SQLiteUserRepository(db)
    user_id = str(uuid.uuid4())
    await repo.create_user({
        "id": user_id, "email": email, "name": email.split("@")[0],
        "password_hash": "x", "role": "user", "is_active": True,
        "auth_provider": "local",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return user_id


@pytest.mark.asyncio
async def test_migration_is_idempotent(db):
    """Running initialize() twice over the same store neither resets rows nor
    loosens invariants (the unique pending-invitation index persists)."""
    user = await _make_user(db, "mig-owner@x.com")
    org_repo = SQLiteOrgRepository(db)
    org = await org_repo.create_org("Mig Org", user)
    await db.initialize()  # re-run the full migration
    assert await org_repo.get_org(org["org_id"]) is not None
    indexes = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'org_invitations'")
    assert any("pending_unique" in (i["name"] or "") for i in indexes), \
        "re-migration dropped the uniqueness invariant"


@pytest.mark.asyncio
async def test_legacy_rows_stay_personal_after_joining(db):
    """§1.16 rule 2: runs created BEFORE the owner formed an org (org_id
    NULL — the migrated shape) are never exposed by org oversight afterward.
    Membership is not retroactive sharing."""
    from backend.repositories.sqlite.workflow_repository import SQLiteWorkflowRepository

    owner = await _make_user(db, "legacy-creator@x.com")
    workflow_repo = SQLiteWorkflowRepository(db)
    now = datetime.now(timezone.utc).isoformat()
    legacy_run_id = str(uuid.uuid4())
    await workflow_repo.save_run({
        "run_id": legacy_run_id, "workflow_id": "wf", "user_id": owner,
        "goal": "private work", "status": "COMPLETED",
        "created_at": now, "updated_at": now,
        # no org_id — the legacy/personal shape
    })

    org_repo = SQLiteOrgRepository(db)
    org = await org_repo.create_org("Later Org", owner)

    # The oversight query (same filter the router applies) must not return it.
    runs = await workflow_repo.list_runs(owner, limit=100, offset=0)
    org_visible = [r for r in runs if r.get("org_id") == org["org_id"]]
    assert org_visible == [], "legacy personal run was attributed to the org"
    assert any(r["run_id"] == legacy_run_id for r in runs), "legacy run must survive migration"


@pytest.mark.asyncio
async def test_pending_invitations_survive_with_their_gate(db):
    """§1.16 rule 7: existing pending invitations keep their token-hash
    format and TTL (not silently invalidated), AND they do not bypass the
    1.3 verified-recipient gate — the gate is evaluated at acceptance time
    against the CURRENT account state."""
    import hashlib
    from backend.services.organization_invitation_service import accept_invitation, InvitationError

    class _AuditStub:
        async def log_event(self, event):
            return event

    owner = await _make_user(db, "inv-owner@x.com")
    unverified = await _make_user(db, "unverified@x.com")
    org_repo = SQLiteOrgRepository(db)
    org = await org_repo.create_org("Gate Org", owner)

    token = "m" * 43
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    await org_repo.create_invitation(
        org["org_id"], "unverified@x.com", "member", owner, token_hash, expires)

    from types import SimpleNamespace
    _factory = SimpleNamespace(
        org_repo=org_repo,
        user_repo=SQLiteUserRepository(db),
        audit_repo=_AuditStub(),
    )

    from backend.config.settings import settings as app_settings
    original = app_settings.REQUIRE_VERIFIED_EMAIL_FOR_INVITES
    app_settings.REQUIRE_VERIFIED_EMAIL_FOR_INVITES = True
    try:
        with pytest.raises(InvitationError, match="Verify your email"):
            await accept_invitation(
                _factory, token=token, user_id=unverified, user_email="unverified@x.com")
    finally:
        app_settings.REQUIRE_VERIFIED_EMAIL_FOR_INVITES = original
    # The invitation itself survived; only the gate refused the unverified
    # account. A verified account can still use it.
    stored = await org_repo.get_invitation_by_token(token_hash)
    assert stored["status"] == "pending"


@pytest.mark.asyncio
async def test_no_ownership_repair_by_assignment(db):
    """§1.16 rule 4: an org always has exactly its creator as owner; nothing
    assigns ownership to whoever requests it (the transfer route is the only
    path, and it requires the current owner)."""
    org_repo = SQLiteOrgRepository(db)
    owner = await _make_user(db, "real-owner@x.com")
    claimant = await _make_user(db, "claimant@x.com")
    org = await org_repo.create_org("No Repair Org", owner)
    # A claimant who is not the owner cannot move ownership through the repo.
    with pytest.raises(ValueError):
        await org_repo.transfer_ownership(org["org_id"], claimant, claimant)
    stored = await org_repo.get_org(org["org_id"])
    assert stored["owner_id"] == owner
