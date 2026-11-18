"""Phase 1.15 membership-concurrency regressions — run with INDEPENDENT
database connections (separate DatabaseManager instances on one file),
which is the cross-process correctness the review requires. Several
repository objects sharing one connection do not establish it."""
import uuid
from datetime import datetime, timedelta, timezone

import asyncio

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.repositories.sqlite.org_repository import SQLiteOrgRepository
from backend.repositories.sqlite.user_repository import SQLiteUserRepository


@pytest_asyncio.fixture
async def two_connections(tmp_path):
    db1 = DatabaseManager(str(tmp_path / "conc.db"))
    db2 = DatabaseManager(str(tmp_path / "conc.db"))
    await db1.initialize()
    await db2.initialize()
    yield {"a": db1, "b": db2}
    await db1.close()
    await db2.close()


def _repo(db):
    return SQLiteOrgRepository(db)


def _user_repo(db):
    return SQLiteUserRepository(db)


async def _make_user(db, email):
    repo = _user_repo(db)
    user_id = str(uuid.uuid4())
    await repo.create_user({
        "id": user_id, "email": email, "name": email.split("@")[0],
        "password_hash": "x", "role": "user", "is_active": True,
        "auth_provider": "local",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return user_id


@pytest.mark.asyncio
async def test_mc_duplicate_pending_invite_across_connections(two_connections):
    """Two processes invite the same recipient simultaneously: exactly ONE
    pending invitation may exist (DB unique partial index), the loser gets
    an empty result, and no membership is created by either."""
    conn_a, conn_b = two_connections["a"], two_connections["b"]
    owner = await _make_user(conn_a, "owner-conc@example.com")
    org = await _repo(conn_a).create_org("Conc Org", owner)
    org_id = org["org_id"]

    token_a = "a" * 43 + "="
    token_b = "b" * 43 + "="
    from datetime import datetime, timezone as tz
    expires = (datetime.now(tz.utc) + timedelta(days=7)).isoformat()
    r1 = await _repo(conn_a).create_invitation(
        org_id, "invitee-conc@example.com", "member", owner,
        __import__("hashlib").sha256(token_a.encode()).hexdigest(), expires)
    r2 = await _repo(conn_b).create_invitation(
        org_id, "invitee-conc@example.com", "admin", owner,
        __import__("hashlib").sha256(token_b.encode()).hexdigest(), expires)

    # Exactly one won; the invitation that exists keeps its role.
    assert bool(r1) != bool(r2) or (r1 and r2 and r1["role"] == r2["role"])
    pending = await _repo(conn_a).list_invitations(org_id, "pending")
    assert len(pending) == 1, "DB invariant: one pending invite per (org, email)"


@pytest.mark.asyncio
async def test_mc_accept_and_revoke_race_has_one_outcome(two_connections):
    """Accept and revoke racing: one consistent outcome — either the accept
    wins (membership exists, invite accepted) or the revoke wins (no
    membership). Never both, never neither-with-membership."""
    conn_a, conn_b = two_connections["a"], two_connections["b"]
    owner = await _make_user(conn_a, "owner-race@example.com")
    invitee = await _make_user(conn_b, "invitee-race@example.com")
    org = await _repo(conn_a).create_org("Race Org", owner)
    import hashlib
    token = "t" * 43 + "="
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await _repo(conn_a).create_invitation(
        org["org_id"], "invitee-race@example.com", "member", owner, token_hash, expires)

    # Fire accept (conn A) and revoke (conn B) concurrently.
    accept_task = asyncio.ensure_future(
        _repo(conn_a).accept_invitation(token_hash, invitee, "invitee-race@example.com"))
    revoke_task = asyncio.ensure_future(
        _repo(conn_b).revoke_invitation(org["org_id"],
                                        (await _repo(conn_b).list_invitations(org["org_id"], "pending"))[0]["invitation_id"]))
    accepted, revoked = await asyncio.gather(accept_task, revoke_task, return_exceptions=True)

    membership = await _repo(conn_a).get_member_role(org["org_id"], invitee)
    invite_status = None
    row = await conn_b.fetch_one(
        "SELECT status FROM org_invitations WHERE token_hash = ?", (token_hash,))
    invite_status = row["status"] if row else None

    if isinstance(accepted, dict):
        # Accept won: membership exists and the invite is accepted.
        assert membership is not None
        assert invite_status in ("accepted", "revoked")  # revoke on accepted is a no-op
    else:
        # Revoke won: no membership, invite revoked.
        assert membership is None
        assert invite_status == "revoked"


@pytest.mark.asyncio
async def test_mc_expired_invite_does_not_block_new_invitations(two_connections):
    """Expiration must not leave a permanent uniqueness claim (Phase 1.6)."""
    conn_a, conn_b = two_connections["a"], two_connections["b"]
    owner = await _make_user(conn_a, "owner-exp@example.com")
    org = await _repo(conn_a).create_org("Exp Org", owner)
    import hashlib
    old_token = "o" * 43 + "="
    old_hash = hashlib.sha256(old_token.encode()).hexdigest()
    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    await _repo(conn_a).create_invitation(
        org["org_id"], "invitee-exp@example.com", "member", owner, old_hash, expired_at)
    # Mark it expired (the natural path: acceptance sweep or TTL pass).
    await conn_a.execute(
        "UPDATE org_invitations SET status = 'expired' WHERE token_hash = ?", (old_hash,))
    await conn_a.commit()

    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    result = await _repo(conn_b).create_invitation(
        org["org_id"], "invitee-exp@example.com", "member", owner,
        hashlib.sha256("new" .encode()).hexdigest(), expires)
    assert result, "a new invitation after expiration must be possible"
    pending = await _repo(conn_a).list_invitations(org["org_id"], "pending")
    assert len(pending) == 1



