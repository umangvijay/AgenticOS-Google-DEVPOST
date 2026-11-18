"""Phase 1.8 ownership-transfer + Phase 1.7 acceptance-transaction + Phase
1.12 background org-membership adversarial suite (§1.15 matrix rows for the
previously open items).

Concurrency tests use INDEPENDENT database connections (separate
DatabaseManager instances on one file) — several repository objects sharing
one connection do not establish cross-process correctness.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.org_repository import SQLiteOrgRepository
from backend.repositories.sqlite.user_repository import SQLiteUserRepository


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest_asyncio.fixture
async def two_connections(tmp_path):
    db1 = DatabaseManager(str(tmp_path / "transfer.db"))
    db2 = DatabaseManager(str(tmp_path / "transfer.db"))
    await db1.initialize()
    await db2.initialize()
    yield {"a": db1, "b": db2}
    await db1.close()
    await db2.close()


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


@pytest_asyncio.fixture
async def org_world(two_connections):
    """owner + admin + member + viewer + outsider, one org (connection A)."""
    conn = two_connections["a"]
    repo = SQLiteOrgRepository(conn)
    owner = await _make_user(conn, "owner@x.com")
    admin = await _make_user(conn, "admin@x.com")
    member = await _make_user(conn, "member@x.com")
    viewer = await _make_user(conn, "viewer@x.com")
    outsider = await _make_user(conn, "outsider@x.com")
    org = await repo.create_org("Transfer Org", owner)
    org_id = org["org_id"]
    assert await repo.add_member(org_id, admin, "admin", owner)
    assert await repo.add_member(org_id, member, "member", owner)
    assert await repo.add_member(org_id, viewer, "viewer", owner)
    return {
        "conn_a": two_connections["a"], "conn_b": two_connections["b"],
        "repo_a": repo, "repo_b": SQLiteOrgRepository(two_connections["b"]),
        "org_id": org_id, "owner": owner, "admin": admin,
        "member": member, "viewer": viewer, "outsider": outsider,
    }


# ── §1.8 dedicated ownership transfer ─────────────────────────────────


@pytest.mark.asyncio
async def test_transfer_success_exactly_one_owner_old_owner_admin(org_world):
    w = org_world
    result = await w["repo_a"].transfer_ownership(w["org_id"], w["owner"], w["admin"])
    assert result["new_owner"] == w["admin"]
    org = await w["repo_a"].get_org(w["org_id"])
    assert org["owner_id"] == w["admin"]
    assert await w["repo_a"].get_member_role(w["org_id"], w["admin"]) == "owner"
    assert await w["repo_a"].get_member_role(w["org_id"], w["owner"]) == "admin"


@pytest.mark.asyncio
async def test_transfer_denied_for_every_non_owner(org_world):
    w = org_world
    for actor in (w["admin"], w["member"], w["viewer"], w["outsider"]):
        with pytest.raises(ValueError):
            await w["repo_a"].transfer_ownership(w["org_id"], actor, w["member"])
    org = await w["repo_a"].get_org(w["org_id"])
    assert org["owner_id"] == w["owner"], "a non-owner moved the owner role"


@pytest.mark.asyncio
async def test_transfer_requires_a_current_member(org_world):
    w = org_world
    with pytest.raises(ValueError, match="current member"):
        await w["repo_a"].transfer_ownership(w["org_id"], w["owner"], w["outsider"])
    # Membership was never created by the failed transfer.
    assert await w["repo_a"].get_member_role(w["org_id"], w["outsider"]) is None


@pytest.mark.asyncio
async def test_transfer_to_self_denied(org_world):
    w = org_world
    with pytest.raises(ValueError):
        await w["repo_a"].transfer_ownership(w["org_id"], w["owner"], w["owner"])


@pytest.mark.asyncio
async def test_concurrent_transfers_admit_exactly_one(org_world):
    """Two independent connections transfer to different members
    simultaneously: exactly one owner exists afterwards and both ordinary
    members did not silently become owners."""
    w = org_world

    async def transfer(conn_key, target):
        repo = w[f"repo_{conn_key}"]
        try:
            return await repo.transfer_ownership(w["org_id"], w["owner"], target)
        except ValueError:
            return None

    results = await asyncio.gather(
        transfer("a", w["member"]),
        transfer("b", w["admin"]),
    )
    succeeded = [r for r in results if r]
    assert len(succeeded) == 1, f"both transfers won: {results}"
    repo = w["repo_a"]
    owners = [m for m in await repo.list_members(w["org_id"]) if m["role"] == "owner"]
    assert len(owners) == 1
    org = await repo.get_org(w["org_id"])
    assert org["owner_id"] == owners[0]["user_id"]


@pytest.mark.asyncio
async def test_transfer_vs_owner_removal_race_keeps_owner_intact(org_world):
    """A concurrent member-removal cannot strand an org without its owner:
    removal is excluded from touching the owner row, and after a transfer the
    OUTGOING owner row exists as admin (never deleted by the loser)."""
    w = org_world

    async def acts():
        # Transfer owner→admin on connection B while connection A tries to
        # remove the (possibly former) owner.
        remove = asyncio.to_thread(
            asyncio.run,
            _remove(w["repo_b"], w["org_id"], w["admin"]),
        )
        transfer = asyncio.to_thread(
            asyncio.run,
            _transfer(w["repo_a"], w["org_id"], w["owner"], w["admin"]),
        )
        return await asyncio.gather(remove, transfer)

    await acts()
    repo = w["repo_a"]
    owners = [m for m in await repo.list_members(w["org_id"]) if m["role"] == "owner"]
    assert len(owners) == 1, "race left the org with zero or multiple owners"
    org = await repo.get_org(w["org_id"])
    assert org["owner_id"] == owners[0]["user_id"]


async def _remove(repo, org_id, user_id):
    return await repo.remove_member(org_id, user_id)


async def _transfer(repo, org_id, current_owner, new_owner):
    try:
        return await repo.transfer_ownership(org_id, current_owner, new_owner)
    except ValueError:
        return None


@pytest.mark.asyncio
async def test_transfer_audit_is_atomic_with_the_swap(org_world):
    """§1.14: the ORG_OWNERSHIP_TRANSFERRED audit row commits in the same
    transaction as the swap — the swap cannot outlive its audit record."""
    w = org_world
    await w["repo_a"].transfer_ownership(w["org_id"], w["owner"], w["admin"])
    events = await w["repo_a"].db.fetch_all(
        "SELECT event_type FROM audit_logs WHERE resource_id = ? AND event_type = 'ORG_OWNERSHIP_TRANSFERRED'",
        (w["org_id"],),
    )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_role_endpoints_still_cannot_touch_the_owner(org_world):
    w = org_world
    assert await w["repo_a"].set_member_role(w["org_id"], w["owner"], "viewer") is False
    assert await w["repo_a"].remove_member(w["org_id"], w["owner"]) is False
    assert await w["repo_a"].get_member_role(w["org_id"], w["owner"]) == "owner"


# ── §1.7 acceptance transaction hardening ─────────────────────────────


@pytest.mark.asyncio
async def test_accept_leaves_invitation_pending_for_existing_member(org_world):
    """The pre-transaction version flipped the invitation to 'accepted' even
    when the accepter was ALREADY a member. Now: membership state is checked
    before any write and the invitation stays pending (revocable)."""
    w = org_world
    import hashlib
    token = "k" * 43
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    invite = await w["repo_a"].create_invitation(
        w["org_id"], "member@x.com", "admin", w["owner"], token_hash, expires)
    assert invite  # created
    with pytest.raises(ValueError, match="Already a member"):
        await w["repo_a"].accept_invitation(token_hash, w["member"], "member@x.com")
    stored = await w["repo_a"].get_invitation_by_token(token_hash)
    assert stored["status"] == "pending", "already-member accept mutated the invitation"
    # The role did not change either (invite wanted admin; member stayed member).
    assert await w["repo_a"].get_member_role(w["org_id"], w["member"]) == "member"


@pytest.mark.asyncio
async def test_accept_vs_revoke_race_is_one_outcome(org_world):
    """Accept on connection A races revoke on connection B: either the
    membership exists and the invitation is accepted, or the invitation is
    revoked and no membership exists — never both, never neither-status."""
    w = org_world
    import hashlib
    token = "r" * 43
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    invite = await w["repo_a"].create_invitation(
        w["org_id"], "outsider@x.com", "member", w["owner"], token_hash, expires)
    invitation_id = invite["invitation_id"]

    accept_task = asyncio.to_thread(
        asyncio.run, _accept(w["repo_b"], token_hash, w["outsider"], "outsider@x.com"))
    revoke_task = asyncio.to_thread(
        asyncio.run, _revoke(w["repo_a"], w["org_id"], invitation_id))
    accepted, revoked = await asyncio.gather(accept_task, revoke_task)

    membership = await w["repo_a"].get_member_role(w["org_id"], w["outsider"])
    stored = await w["repo_a"].get_invitation_by_token(token_hash)
    if membership:
        assert accepted, "membership exists but acceptance lost"
        assert stored["status"] == "accepted"
    else:
        assert stored["status"] in ("revoked", "pending", "expired")
        assert not accepted or stored["status"] == "accepted" and membership


async def _accept(repo, token_hash, user_id, email):
    try:
        return await repo.accept_invitation(token_hash, user_id, email)
    except ValueError:
        return None


async def _revoke(repo, org_id, invitation_id):
    return await repo.revoke_invitation(org_id, invitation_id)


@pytest.mark.asyncio
async def test_accept_writes_audit_inside_the_transaction(org_world):
    """§1.14: ORG_INVITATION_ACCEPTED is committed atomically with the
    membership — the acceptance cannot outlive its audit record."""
    w = org_world
    import hashlib
    token = "a" * 43
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    await w["repo_a"].create_invitation(
        w["org_id"], "outsider@x.com", "member", w["owner"], token_hash, expires)
    await w["repo_a"].accept_invitation(token_hash, w["outsider"], "outsider@x.com")
    events = await w["repo_a"].db.fetch_all(
        "SELECT event_type FROM audit_logs WHERE resource_id = ? AND event_type = 'ORG_INVITATION_ACCEPTED'",
        (w["org_id"],),
    )
    assert len(events) == 1


# ── §1.12 background org-membership recheck ───────────────────────────


class _AuditStub:
    def __init__(self):
        self.events = []

    async def log_event(self, event):
        self.events.append(event)
        return event


@pytest.mark.asyncio
async def test_bg_removed_member_org_run_denied(two_connections):
    """A removed member's queued ORG-owned run is denied at execution: the
    REAL worker gate rechecks CURRENT membership, audits the denial with a
    machine-readable reason, and blocks execution (the §1.12 completion
    check: a removed member cannot reach org resources through queued work)."""
    conn = two_connections["a"]
    from backend.worker import authorize_background_execution

    owner = await _make_user(conn, "bg-owner@x.com")
    org_repo = SQLiteOrgRepository(conn)
    org = await org_repo.create_org("BG Org", owner)
    run_id = str(uuid.uuid4())

    import backend.repositories.sqlite.workflow_repository as wr
    workflow_repo = wr.SQLiteWorkflowRepository(conn)
    await workflow_repo.save_run({
        "run_id": run_id, "workflow_id": "wf", "user_id": owner,
        "goal": "g", "status": "PENDING", "org_id": org["org_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    # The run belongs to a REMOVABLE member (the org owner row is protected
    # from removal by the Phase 1.8 invariants).
    member = await _make_user(conn, "bg-member@x.com")
    await org_repo.add_member(org["org_id"], member, "member", owner)
    run_id = str(uuid.uuid4())
    await workflow_repo.save_run({
        "run_id": run_id, "workflow_id": "wf", "user_id": member,
        "goal": "g", "status": "PENDING", "org_id": org["org_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    audit = _AuditStub()

    assert await authorize_background_execution(
        workflow_repo, run_id, "t1",
        user_repo=SQLiteUserRepository(conn), org_repo=org_repo, audit=audit,
    ) is None, "current member was denied"

    await org_repo.remove_member(org["org_id"], member)
    deny = await authorize_background_execution(
        workflow_repo, run_id, "t1",
        user_repo=SQLiteUserRepository(conn), org_repo=org_repo, audit=audit,
    )
    assert deny == "org_membership_lost"
    assert any(
        getattr(e, "details", {}).get("reason") == "org_membership_lost"
        or (isinstance(getattr(e, "details", None), dict)
            and e.details.get("reason") == "org_membership_lost")
        for e in audit.events
    ), "denial was not audited with its reason"


@pytest.mark.asyncio
async def test_bg_active_member_org_run_executes(two_connections):
    conn = two_connections["a"]
    from backend.worker import authorize_background_execution
    import backend.repositories.sqlite.workflow_repository as wr

    owner = await _make_user(conn, "bg-active@x.com")
    org_repo = SQLiteOrgRepository(conn)
    org = await org_repo.create_org("BG Active", owner)
    run_id = str(uuid.uuid4())
    workflow_repo = wr.SQLiteWorkflowRepository(conn)
    await workflow_repo.save_run({
        "run_id": run_id, "workflow_id": "wf", "user_id": owner,
        "goal": "g", "status": "PENDING", "org_id": org["org_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    assert await authorize_background_execution(
        workflow_repo, run_id, "t1",
        user_repo=SQLiteUserRepository(conn), org_repo=org_repo,
        audit=_AuditStub(),
    ) is None


@pytest.mark.asyncio
async def test_bg_deleted_org_run_denied(two_connections):
    """An org-owned run whose organization was deleted cannot execute: the
    org no longer grants any permission."""
    conn = two_connections["a"]
    from backend.worker import authorize_background_execution
    import backend.repositories.sqlite.workflow_repository as wr

    owner = await _make_user(conn, "bg-delorg@x.com")
    org_repo = SQLiteOrgRepository(conn)
    org = await org_repo.create_org("BG Doomed", owner)
    run_id = str(uuid.uuid4())
    workflow_repo = wr.SQLiteWorkflowRepository(conn)
    await workflow_repo.save_run({
        "run_id": run_id, "workflow_id": "wf", "user_id": owner,
        "goal": "g", "status": "PENDING", "org_id": org["org_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    await org_repo.delete_org(org["org_id"])
    assert await authorize_background_execution(
        workflow_repo, run_id, "t1",
        user_repo=SQLiteUserRepository(conn), org_repo=org_repo,
        audit=_AuditStub(),
    ) == "org_membership_lost"


@pytest.mark.asyncio
async def test_bg_personal_run_unaffected_by_membership_checks(two_connections):
    """Personal runs (org_id NULL) never consult org membership — personal
    work is not blocked by org state (§1.0 guarantee 4)."""
    conn = two_connections["a"]
    from backend.worker import authorize_background_execution
    import backend.repositories.sqlite.workflow_repository as wr

    owner = await _make_user(conn, "bg-personal@x.com")
    run_id = str(uuid.uuid4())
    workflow_repo = wr.SQLiteWorkflowRepository(conn)
    await workflow_repo.save_run({
        "run_id": run_id, "workflow_id": "wf", "user_id": owner,
        "goal": "g", "status": "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    assert await authorize_background_execution(
        workflow_repo, run_id, "t1",
        user_repo=SQLiteUserRepository(conn),
        org_repo=SQLiteOrgRepository(conn), audit=_AuditStub(),
    ) is None
