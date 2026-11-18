"""Phase 9 §9.21 concurrency + §9.0D acceptance: browser session ownership
races with concurrent calls, duplicate-resume allocation checks, and
business-action identity stability across ownership changes.
"""
import asyncio
import time

import pytest
import pytest_asyncio

from backend.models.browser_session import BrowserSession
from backend.repositories.browser_session_repository import BrowserSessionRepository
from backend.services.browser_runtime import build_action_intent, ActionKind


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "conc.db"))
    await db.initialize()
    yield BrowserSessionRepository(db)
    await db.close()


def _session(**kw):
    defaults = dict(session_id="bs_c", tenant_id="u1", user_id="u1",
                    run_id="run-c", task_id="task-c", lease_expires_at=0)
    defaults.update(kw)
    return BrowserSession(**defaults)


@pytest.mark.asyncio
async def test_four_workers_race_one_wins(repo):
    """§9.21: four workers race CONCURRENTLY — exactly one wins."""
    await repo.create_session(_session())
    results = await asyncio.gather(*[
        repo.acquire_ownership("bs_c", f"worker-{i}", "u1", 60, i)
        for i in range(4)])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].owner_generation == 1


@pytest.mark.asyncio
async def test_sequential_reacquisition_advances_generation(repo):
    """§9.0A: each reacquisition advances the server-controlled generation —
    a caller cannot reuse an old epoch by repeating an attempt number."""
    await repo.create_session(_session())
    epochs = []
    for attempt in range(1, 4):
        await repo.db.execute(
            "UPDATE browser_sessions SET lease_expires_at=0 WHERE session_id='bs_c'")
        await repo.db.commit()
        s = await repo.acquire_ownership("bs_c", f"w-{attempt}", "u1", 30, attempt)
        epochs.append(s.owner_epoch)
    assert len(set(epochs)) == 3   # all distinct — monotonically advancing


@pytest.mark.asyncio
async def test_business_action_identity_stable_across_ownership(repo):
    """§9.0D: the same business action (same target+payload, different
    ownership epoch) maps to the SAME effect — a recovery attempt keeps the
    original effect identity."""
    from backend.repositories.effect_repository import SQLiteEffectRepository
    from backend.repositories.sqlite.database import DatabaseManager
    import os
    db = DatabaseManager(os.path.join(str(repo.db.db_path).replace(".db", "-fx.db")))
    await db.initialize()
    fx = SQLiteEffectRepository(db)
    try:
        # Attempt 1 (worker-1, epoch-1): records the business action.
        i1 = build_action_intent(
            session_id="bs_c", controller_epoch="epoch-1",
            frame_origin="https://x.io", action_kind=ActionKind.SUBMIT,
            target_identity="#send", material_payload={"msg": "go"})
        from backend.services.browser_effect_service import record_browser_action_effect
        r1 = await record_browser_action_effect(fx, intent=i1, owner_user_id="u1")
        assert r1["created"] is True

        # Ownership changes (worker-2 recovers), but the BUSINESS action is
        # the same (material digest excludes the epoch — §9.0D).
        i2 = build_action_intent(
            session_id="bs_c", controller_epoch="epoch-2",
            frame_origin="https://x.io", action_kind=ActionKind.SUBMIT,
            target_identity="#send", material_payload={"msg": "go"})
        r2 = await record_browser_action_effect(fx, intent=i2, owner_user_id="u1")
        assert r2["created"] is False
        assert r2["effect_id"] == r1["effect_id"]   # SAME effect — recovery, not duplication

        # A DELIBERATELY different action (different payload) gets a NEW effect.
        i3 = build_action_intent(
            session_id="bs_c", controller_epoch="epoch-2",
            frame_origin="https://x.io", action_kind=ActionKind.SUBMIT,
            target_identity="#send", material_payload={"msg": "go again"})
        r3 = await record_browser_action_effect(fx, intent=i3, owner_user_id="u1")
        assert r3["created"] is True
        assert r3["effect_id"] != r1["effect_id"]
    finally:
        await db.close()
