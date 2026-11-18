"""Phase 14 §14.5: browser session state CHECKPOINTS are durable.

After every successful executed action the runtime records a signed
checkpoint reference on the durable session (digest + milestone +
capture time); recovery and verification read THIS reference, never a
model's claim about where the session got to. Checkpoints advance
monotonically — an older observation never overwrites a newer one.
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "ckpt.db"))
    await db.initialize()
    from backend.repositories.browser_session_repository import (
        BrowserSessionRepository)
    from backend.models.browser_session import (
        BrowserSession, ControllerState, ResourceState, ChallengeState,
        CleanupState)
    r = BrowserSessionRepository(db)
    session = await r.create_session(BrowserSession(
        session_id="bs-ckpt-1", tenant_id="t1", user_id="u1",
        workflow_id="wf1", run_id="run-ck", task_id="task-ck",
        owner_worker_id="w1", owner_epoch="e1",
        resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT,
        challenge_state=ChallengeState.NONE,
        cleanup_state=CleanupState.NOT_REQUIRED,
        allowed_origins=["acme.test"], runtime_session_id="rt-1",
        policy_revision="p1"))
    try:
        yield {"repo": r, "db": db, "session": session}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_checkpoint_is_recorded_and_readable(repo):
    r = repo["repo"]
    ref = await r.record_checkpoint(
        "bs-ckpt-1", checkpoint_digest="abc123", milestone="step 3: submit form",
        captured_at=1000)
    assert ref
    stored = await r.get_session("bs-ckpt-1")
    assert stored.checkpoint_reference == ref, \
        "the durable session carries the checkpoint reference"


@pytest.mark.asyncio
async def test_checkpoints_advance_monotonically(repo):
    """A STALE observation never overwrites a newer checkpoint."""
    r = repo["repo"]
    ref1 = await r.record_checkpoint(
        "bs-ckpt-1", checkpoint_digest="state-1", milestone="step 1",
        captured_at=1000)
    ref2 = await r.record_checkpoint(
        "bs-ckpt-1", checkpoint_digest="state-2", milestone="step 2",
        captured_at=2000)
    # An out-of-order (older) delivery is refused.
    ref_stale = await r.record_checkpoint(
        "bs-ckpt-1", checkpoint_digest="state-0", milestone="step 0",
        captured_at=500)
    stored = await r.get_session("bs-ckpt-1")
    assert stored.checkpoint_reference == ref2, \
        "the NEWEST observation is the session's checkpoint"
    assert ref_stale != ref2 and ref2.startswith(ref2.split(":")[0])


@pytest.mark.asyncio
async def test_same_tick_checkpoint_still_advances(repo):
    """Two observations captured in the same tick: the later write still
    lands (the monotonic guard is <=, not <)."""
    r = repo["repo"]
    await r.record_checkpoint("bs-ckpt-1", checkpoint_digest="a",
                              milestone="m1", captured_at=1000)
    ref2 = await r.record_checkpoint("bs-ckpt-1", checkpoint_digest="b",
                                     milestone="m2", captured_at=1000)
    stored = await r.get_session("bs-ckpt-1")
    assert stored.checkpoint_reference == ref2


@pytest.mark.asyncio
async def test_first_checkpoint_replaces_the_empty_reference(repo):
    r = repo["repo"]
    before = await r.get_session("bs-ckpt-1")
    assert not before.checkpoint_reference
    ref = await r.record_checkpoint("bs-ckpt-1", checkpoint_digest="d1",
                                    milestone="first", captured_at=100)
    stored = await r.get_session("bs-ckpt-1")
    assert stored.checkpoint_reference == ref
