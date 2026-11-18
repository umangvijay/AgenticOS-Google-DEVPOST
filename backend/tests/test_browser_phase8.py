"""Phase 8 §8.24 acceptance tests: browser session ownership, epoch
fencing, network policy, credential injection, takeover grants, recovery
levels, action-intent material binding, effect integration, and the
lifecycle transition matrix — through service/repo boundaries.
"""
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio

from backend.models.effect import EffectState
from backend.models.browser_session import (
    Actor, BrowserSession, ChallengeState, ControllerState, CleanupState,
    ResourceState, SessionTransitionError, ownership_epoch,
    session_transition_valid,
)
from backend.repositories.browser_session_repository import BrowserSessionRepository
from backend.services.browser_runtime import (
    ActionKind, BrowserNetworkPolicy, RecoveryLevel, build_action_intent,
    determine_recovery_level, evaluate_credential_injection,
    issue_takeover_grant, revoke_takeover_grant,
)
from backend.services.browser_effect_service import record_browser_action_effect


@pytest_asyncio.fixture
async def db(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "browser.db"))
    await db.initialize()
    yield db
    await db.close()


@pytest_asyncio.fixture
async def repo(db):
    return BrowserSessionRepository(db)


def _session(**overrides) -> BrowserSession:
    defaults = dict(session_id="bs_test", tenant_id="u1", user_id="u1",
                    run_id="run-1", task_id="task-1",
                    owner_worker_id="worker-A", owner_epoch="epoch-A",
                    lease_expires_at=int(time.time()) + 60)
    defaults.update(overrides)
    return BrowserSession(**defaults)


# ── §8.7: ownership races and epoch fencing ───────────────────────────

@pytest.mark.asyncio
async def test_two_workers_race_one_wins(repo):
    """§8.7/§9.0A acceptance: two workers race CONCURRENTLY to acquire an
    expired session — exactly one wins (a sequence of two ordinary calls is
    not a concurrency test)."""
    import asyncio
    session = _session(lease_expires_at=0)   # expired: both may race
    await repo.create_session(session)
    results = await asyncio.gather(
        repo.acquire_ownership("bs_test", "worker-1", "u1", 60, 1),
        repo.acquire_ownership("bs_test", "worker-2", "u1", 60, 1),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"exactly one winner expected, got {winners}"
    assert winners[0].owner_worker_id in ("worker-1", "worker-2")


@pytest.mark.asyncio
async def test_foreign_tenant_cannot_acquire(repo):
    """§9.0A: a different tenant cannot acquire another tenant's session
    even with an expired lease — enforced in the transaction."""
    await repo.create_session(_session(lease_expires_at=0))
    result = await repo.acquire_ownership("bs_test", "attacker", "tenant-B", 60, 1)
    assert result is None
    rec = await repo.get_session("bs_test")
    assert rec.owner_worker_id == "worker-A"   # unchanged


@pytest.mark.asyncio
async def test_old_worker_fenced_after_ownership_change(repo):
    """§8.7 acceptance: an old worker cannot act after ownership changes."""
    await repo.create_session(_session(lease_expires_at=0))
    first = await repo.acquire_ownership("bs_test", "worker-1", "u1", 60, 1)
    assert first is not None
    epoch_1 = first.owner_epoch
    # The live lease blocks worker-2 while worker-1 holds it.
    assert await repo.acquire_ownership("bs_test", "worker-2", "u1", 60, 2) is None
    # Lease expires; worker-2 takes over → a NEW, monotonically ADVANCED
    # server-controlled generation (not a caller-chosen attempt reuse).
    await repo.db.execute(
        "UPDATE browser_sessions SET lease_expires_at=0 WHERE session_id='bs_test'")
    await repo.db.commit()
    second = await repo.acquire_ownership("bs_test", "worker-2", "u1", 60, 2)
    assert second is not None and second.owner_epoch != epoch_1
    # Old worker's renewal fails (its epoch is obsolete).
    assert await repo.renew_ownership("bs_test", "worker-1", epoch_1, 60) is False
    # New worker's renewal succeeds.
    assert await repo.renew_ownership("bs_test", "worker-2",
                                      second.owner_epoch, 60) is True
    # §9.0A: an EXPIRED lease cannot be revived by renewal — controlled
    # reacquisition (which advances the generation) is required.
    await repo.db.execute(
        "UPDATE browser_sessions SET lease_expires_at=0 WHERE session_id='bs_test'")
    await repo.db.commit()
    assert await repo.renew_ownership("bs_test", "worker-2",
                                      second.owner_epoch, 60) is False


@pytest.mark.asyncio
async def test_duplicate_resume_no_duplicate_browsers(repo):
    """§8.7 acceptance: duplicate resume requests do not create duplicate
    browsers — the same run/task resolves to the SAME session."""
    await repo.create_session(_session())
    a = await repo.get_session_for_run("run-1", "task-1")
    b = await repo.get_session_for_run("run-1", "task-1")
    assert a.session_id == b.session_id


# ── §8.6: lifecycle transition matrix ─────────────────────────────────

def test_transition_matrix_rejects_wrong_actor():
    assert session_transition_valid(ResourceState.READY,
                                    ResourceState.CLOSING, Actor.HUMAN) is True
    assert session_transition_valid(ResourceState.CLOSED,
                                    ResourceState.READY, Actor.SYSTEM) is False
    assert session_transition_valid(ResourceState.FAILED,
                                    ResourceState.READY, Actor.SYSTEM) is False


@pytest.mark.asyncio
async def test_transition_validates_actor_and_state(repo):
    await repo.create_session(_session(resource_state=ResourceState.READY))
    with pytest.raises(SessionTransitionError):
        await repo.transition("bs_test", ResourceState.CLOSED, Actor.AGENT)
    updated = await repo.transition("bs_test", ResourceState.CLOSING,
                                    Actor.AGENT)
    assert updated.resource_state is ResourceState.CLOSING


# ── §8.6 acceptance: controller state visible separately ──────────────

@pytest.mark.asyncio
async def test_human_controller_distinct_from_failure(repo):
    """§8.6 acceptance: READY/HUMAN/MFA (browser alive while user
    authenticates) is different from FAILED/NONE — the frontend cannot show
    'Agent running' while the controller is HUMAN."""
    await repo.create_session(_session(
        resource_state=ResourceState.READY,
        controller_state=ControllerState.HUMAN,
        challenge_state=ChallengeState.MFA))
    session = await repo.get_session("bs_test")
    assert session.resource_state is ResourceState.READY
    assert session.controller_state is ControllerState.HUMAN
    assert session.controller_state is not ControllerState.NONE


# ── §8.10: timers ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_activity_extends_idle_never_absolute(repo):
    """§8.10 acceptance: a session cannot remain alive indefinitely merely
    because its preview stream is open — heartbeats never extend the
    absolute lifetime."""
    await repo.create_session(_session(
        resource_state=ResourceState.READY,
        idle_expires_at=int(time.time()) + 60,
        absolute_expires_at=int(time.time()) + 300))
    await repo.record_activity("bs_test", extend_idle_seconds=600)
    session = await repo.get_session("bs_test")
    assert session.idle_expires_at > int(time.time()) + 500
    assert session.absolute_expires_at == int(time.time()) + 300  # unchanged


@pytest.mark.asyncio
async def test_expired_sessions_are_sweep_candidates(repo):
    await repo.create_session(_session(
        resource_state=ResourceState.READY,
        idle_expires_at=int(time.time()) - 10))
    await repo.create_session(_session(
        session_id="bs_active", resource_state=ResourceState.READY,
        idle_expires_at=int(time.time()) + 600))
    expired = await repo.expired_sessions()
    ids = [s.session_id for s in expired]
    assert "bs_test" in ids and "bs_active" not in ids


# ── §8.11: network policy ─────────────────────────────────────────────

def test_network_policy_separates_navigation_from_resources():
    """§8.11 acceptance: a permitted page cannot send data to an unapproved
    destination just because the request is not a navigation."""
    policy = BrowserNetworkPolicy(
        policy_id="p1", navigation_origins=["https://app.example.com"],
        api_origins=["https://api.example.com"])
    assert policy.navigation_allowed("https://app.example.com") is True
    # Third-party resource from a trusted navigation origin: NOT allowed.
    assert policy.resource_allowed("https://tracker.example.com") is False
    assert policy.resource_allowed("https://api.example.com") is True
    # Alternate port is a different origin (normalized).
    assert policy.navigation_allowed("https://app.example.com:8443") is False
    assert policy.websocket_allowed("https://app.example.com") is False


# ── §8.12: credential injection ───────────────────────────────────────

def test_credential_injection_happy_path():
    decision, _ = evaluate_credential_injection(
        owner_user_id="u1", requesting_user_id="u1",
        binding_status="active", binding_owner="u1",
        expected_top_level_origin="https://app.example.com",
        current_top_level_origin="https://app.example.com",
        expected_frame_origin="https://app.example.com",
        current_frame_origin="https://app.example.com",
        field_purpose="password", observed_field_purpose="password",
        controller=ControllerState.AGENT, challenge=ChallengeState.NONE,
        observation_version=3, current_observation_version=3)
    from backend.services.browser_runtime import CredentialInjectionDecision
    assert decision is CredentialInjectionDecision.ALLOWED


def test_page_instructed_secret_in_wrong_field_denied():
    """§8.12 acceptance: a page instructing the agent to place
    {{secret:password}} in a support-message field is rejected."""
    decision, reason = evaluate_credential_injection(
        owner_user_id="u1", requesting_user_id="u1",
        binding_status="active", binding_owner="u1",
        expected_top_level_origin="https://app.example.com",
        current_top_level_origin="https://app.example.com",
        expected_frame_origin="https://app.example.com",
        current_frame_origin="https://app.example.com",
        field_purpose="password", observed_field_purpose="support_message",
        controller=ControllerState.AGENT, challenge=ChallengeState.NONE,
        observation_version=3, current_observation_version=3)
    from backend.services.browser_runtime import CredentialInjectionDecision
    assert decision is CredentialInjectionDecision.DENIED_FIELD_PURPOSE


def test_credential_injection_denied_for_human_challenge_fields():
    decision, _ = evaluate_credential_injection(
        owner_user_id="u1", requesting_user_id="u1",
        binding_status="active", binding_owner="u1",
        expected_top_level_origin="https://app.example.com",
        current_top_level_origin="https://app.example.com",
        expected_frame_origin="https://app.example.com",
        current_frame_origin="https://app.example.com",
        field_purpose="otp", observed_field_purpose="otp",
        controller=ControllerState.HUMAN, challenge=ChallengeState.MFA,
        observation_version=1, current_observation_version=1)
    from backend.services.browser_runtime import CredentialInjectionDecision
    assert decision is CredentialInjectionDecision.DENIED_CHALLENGE_FIELD


# ── §8.13: material action-intent binding ─────────────────────────────

def test_material_change_invalidates_prior_approval():
    """§8.13 acceptance: changing a recipient, amount, resource, or
    submitted message invalidates the previous action approval when that
    detail is material."""
    intent_a = build_action_intent(
        session_id="s1", controller_epoch="e1", frame_origin="https://x.io",
        action_kind=ActionKind.SUBMIT, target_identity="#send",
        material_payload={"recipient": "a@x.io", "amount": 100})
    intent_same = build_action_intent(
        session_id="s1", controller_epoch="e1", frame_origin="https://x.io",
        action_kind=ActionKind.SUBMIT, target_identity="#send",
        material_payload={"recipient": "a@x.io", "amount": 100})
    intent_changed = build_action_intent(
        session_id="s1", controller_epoch="e1", frame_origin="https://x.io",
        action_kind=ActionKind.SUBMIT, target_identity="#send",
        material_payload={"recipient": "b@x.io", "amount": 100})
    assert intent_a.material_digest() == intent_same.material_digest()
    assert intent_a.material_digest() != intent_changed.material_digest()


def test_non_form_writes_classified_consequential():
    """§8.13: delete buttons outside forms, JS publish, settings checkboxes,
    autosave are all consequential kinds — the submit heuristic alone is not
    the policy."""
    from backend.services.browser_runtime import CONSEQUENTIAL_KINDS
    for kind in (ActionKind.DELETE, ActionKind.PUBLISH,
                 ActionKind.SETTINGS_CHANGE, ActionKind.AUTOSAVE,
                 ActionKind.SELECT_TRANSACTION, ActionKind.SUBMIT):
        assert kind in CONSEQUENTIAL_KINDS


# ── §8.14: browser actions through the effect ledger ──────────────────

@pytest_asyncio.fixture
async def effect_repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.effect_repository import SQLiteEffectRepository
    db = DatabaseManager(str(tmp_path / "fx.db"))
    await db.initialize()
    yield SQLiteEffectRepository(db)
    await db.close()


@pytest.mark.asyncio
async def test_browser_effect_recorded_and_restart_no_silent_repeat(effect_repo):
    """§8.14 acceptance: a browser restart cannot silently repeat a
    potentially completed external action."""
    from backend.models.browser_session import ownership_epoch
    intent = build_action_intent(
        session_id="s1", controller_epoch="e1",
        frame_origin="https://app.example.com",
        action_kind=ActionKind.SUBMIT, target_identity="#send",
        material_payload={"message": "publish the report"})
    first = await record_browser_action_effect(
        effect_repo, intent=intent, owner_user_id="u1", run_id="r1")
    assert first["created"] is True
    second = await record_browser_action_effect(
        effect_repo, intent=intent, owner_user_id="u1", run_id="r1")
    assert second["created"] is False
    assert second["reconciliation_required"] is False   # PREPARED: not yet dispatched
    # After dispatch + crash, the uncertain state REQUIRES reconciliation.
    await effect_repo.transition(first["effect_id"], EffectState.DISPATCHING,
                                 actor="browser")
    await effect_repo.transition(first["effect_id"], EffectState.OUTCOME_UNKNOWN,
                                 actor="browser")
    third = await record_browser_action_effect(
        effect_repo, intent=intent, owner_user_id="u1", run_id="r1")
    assert third["created"] is False
    assert third["reconciliation_required"] is True


# ── §8.16: takeover grants ────────────────────────────────────────────

def test_takeover_grant_lifecycle():
    """§8.16 acceptance: a second user cannot use the grant; expired grants
    stop working; revocation closes control; view ≠ control."""
    grant = issue_takeover_grant(session_id="s1", user_id="u1", tenant_id="t1",
                                 controller_epoch="e1", ttl_seconds=60)
    now = int(time.time())
    assert grant.live_for("u1", "s1", now) is True
    assert grant.live_for("attacker", "s1", now) is False     # second user
    assert grant.live_for("u1", "s-other", now) is False      # copied to another session
    expired = issue_takeover_grant(session_id="s1", user_id="u1", tenant_id="t1",
                                   controller_epoch="e1", ttl_seconds=30)
    boundary = expired.expires_at
    # Strict boundary: a grant is dead AT its exact expiration timestamp.
    assert expired.live_for("u1", "s1", boundary) is False
    assert expired.live_for("u1", "s1", boundary - 1) is True
    revoke_takeover_grant(grant)
    assert grant.live_for("u1", "s1", now) is False
    view_only = issue_takeover_grant(session_id="s1", user_id="u1", tenant_id="t1",
                                     controller_epoch="e1", permissions=["view"])
    assert view_only.can_control() is False   # view ≠ control


# ── §8.19: recovery levels ────────────────────────────────────────────

def test_recovery_levels_are_honest():
    """§8.19: saved storage state is not a complete browser snapshot;
    uncertain effects require manual reconciliation before continuation."""
    assert determine_recovery_level(
        live_session_exists=True, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=False) is RecoveryLevel.LIVE_CONTINUATION
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=True,
        storage_state_valid=True, pending_uncertain_effects=False,
        reauth_verifiable=True) is RecoveryLevel.AUTHENTICATED_RECONSTRUCTION
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=True) is RecoveryLevel.REAUTH_REQUIRED
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=True,
        reauth_verifiable=True) is RecoveryLevel.MANUAL_RECONCILIATION_REQUIRED
    assert determine_recovery_level(
        live_session_exists=False, storage_state_available=False,
        storage_state_valid=False, pending_uncertain_effects=False,
        reauth_verifiable=False) is RecoveryLevel.UNSUPPORTED
