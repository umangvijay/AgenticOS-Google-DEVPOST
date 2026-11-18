"""Firestore parity for the Phase 5/8/10/11 durable stores.

Completion condition: the SAME contract tests pass against SQLite and
Firestore. These tests run the effect ledger, browser-session ownership,
execution-step CAS, and recovery-episode claim matrices against the
Firestore implementations whenever a Firestore emulator is configured:

    FIRESTORE_EMULATOR_HOST=localhost:8080 \\
        GOOGLE_CLOUD_PROJECT=demo pytest backend/tests/test_phase8_11_firestore_parity.py

Without an emulator they are skipped (SQLite parity is covered by
test_effect_dispatch.py / test_browser_phase8.py / test_execution_steps.py /
test_recovery_episodes.py, which these tests mirror on purpose).
"""
import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio

_RUN = uuid.uuid4().hex[:8]

_EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()

pytestmark = pytest.mark.skipif(
    not _EMULATOR,
    reason="Firestore parity requires FIRESTORE_EMULATOR_HOST (e.g. localhost:8080)",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_emulator_data():
    """The emulator is persistent across runs — wipe the Phase 5/8/10/11
    collections before each test so assertions never see stale state."""
    from backend.repositories.firestore import database as fs_database
    fs_database.FirestoreDB._instance = None
    try:
        client = await fs_database.FirestoreDB.get_client()
        for name in ("browser_sessions", "execution_steps",
                     "recovery_episodes", "effects", "effect_receipts",
                     "effect_digest_index", "recovery_active_episodes",
                     "evaluation_records", "evaluation_identities",
                     "experiments", "capacity_generations",
                     "prompt_versions", "prompt_activations", "policy_snapshots",
                     "agent_tasks", "agent_task_dedup", "routing_decisions"):
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
async def fs():
    from backend.config.settings import settings as app_settings
    if not app_settings.GOOGLE_CLOUD_PROJECT:
        pytest.skip("Firestore parity requires GOOGLE_CLOUD_PROJECT (any value against the emulator)")
    from backend.repositories.firestore import database as fs_database
    # pytest-asyncio gives each test its own event loop; the cached gRPC
    # client is loop-bound — every test gets a FRESH client.
    fs_database.FirestoreDB._instance = None
    try:
        yield True
    finally:
        client = fs_database.FirestoreDB._instance
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            fs_database.FirestoreDB._instance = None


# ── Browser sessions (§8.7/§9.0A) ─────────────────────────────────────

def _session(**overrides):
    from backend.models.browser_session import BrowserSession
    defaults = dict(
        session_id=f"bs-{_RUN}", tenant_id="u1", user_id="u1",
        run_id=f"run-{_RUN}", task_id="task-1",
        owner_worker_id="worker-A", owner_epoch="epoch-A",
        lease_expires_at=int(time.time()) + 60)
    defaults.update(overrides)
    return BrowserSession(**defaults)


@pytest.mark.asyncio
async def test_browser_ownership_tenant_and_lease_on_firestore(fs):
    """§9.0A on Firestore: a foreign tenant can never acquire; a live
    lease blocks a different worker; the same worker re-acquires with an
    ADVANCED generation."""
    from backend.repositories.firestore.browser_session_repository import (
        FirestoreBrowserSessionRepository)
    repo = FirestoreBrowserSessionRepository()
    sid = f"bs-own-{_RUN}"
    await repo.create_session(_session(session_id=sid, lease_expires_at=0))

    foreign = await repo.acquire_ownership(sid, "worker-B", "u2", 60, 1)
    assert foreign is None, "tenant identity must be enforced in the transaction"

    won = await repo.acquire_ownership(sid, "worker-B", "u1", 60, 1)
    assert won is not None and won.owner_worker_id == "worker-B"
    gen1 = won.owner_generation
    assert gen1 >= 1

    # Live lease blocks a DIFFERENT worker.
    assert await repo.acquire_ownership(sid, "worker-C", "u1", 60, 2) is None
    # The CURRENT worker re-acquires (lease held) with a new generation.
    again = await repo.acquire_ownership(sid, "worker-B", "u1", 60, 3)
    assert again is not None and again.owner_generation > gen1


@pytest.mark.asyncio
async def test_browser_renewal_expiry_rules_on_firestore(fs):
    """§8.7 on Firestore: renewal verifies worker + epoch + NON-EXPIRED
    lease; an expired lease cannot be revived by renewal."""
    from backend.repositories.firestore.browser_session_repository import (
        FirestoreBrowserSessionRepository)
    repo = FirestoreBrowserSessionRepository()
    sid = f"bs-renew-{_RUN}"
    created = await repo.create_session(_session(
        session_id=sid, owner_worker_id="worker-A", owner_epoch="ep-A",
        lease_expires_at=int(time.time()) + 60))
    assert await repo.renew_ownership(sid, "worker-A", "ep-A", 120) is True
    assert await repo.renew_ownership(sid, "worker-B", "ep-A", 120) is False, \
        "wrong worker cannot renew"

    expired = await repo.create_session(_session(
        session_id=sid + "-x", owner_worker_id="worker-A", owner_epoch="ep-A",
        lease_expires_at=0))
    assert expired.lease_expires_at == 0
    assert await repo.renew_ownership(sid + "-x", "worker-A", "ep-A", 120) is False, \
        "expired lease cannot be revived by renewal"


@pytest.mark.asyncio
async def test_browser_transition_matrix_on_firestore(fs):
    from backend.models.browser_session import Actor, ResourceState, SessionTransitionError
    from backend.repositories.firestore.browser_session_repository import (
        FirestoreBrowserSessionRepository)
    repo = FirestoreBrowserSessionRepository()
    sid = f"bs-tr-{_RUN}"
    await repo.create_session(_session(session_id=sid, resource_state=ResourceState.READY))

    moved = await repo.transition(sid, ResourceState.RECOVERING, Actor.SYSTEM)
    assert moved.resource_state is ResourceState.RECOVERING

    from backend.models.browser_session import ALLOWED_SESSION_TRANSITIONS
    legal = ALLOWED_SESSION_TRANSITIONS.get(ResourceState.CLOSED, {})
    assert not legal, "CLOSED must be terminal"

    with pytest.raises(SessionTransitionError):
        # ALLOCATING is not reachable from RECOVERING (not in the matrix).
        await repo.transition(sid, ResourceState.ALLOCATING, Actor.SYSTEM)


@pytest.mark.asyncio
async def test_browser_expired_sweep_on_firestore(fs):
    from backend.models.browser_session import ResourceState
    from backend.repositories.firestore.browser_session_repository import (
        FirestoreBrowserSessionRepository)
    repo = FirestoreBrowserSessionRepository()
    await repo.create_session(_session(
        session_id=f"bs-sweep-{_RUN}", resource_state=ResourceState.READY,
        idle_expires_at=1))
    await repo.create_session(_session(
        session_id=f"bs-live-{_RUN}", resource_state=ResourceState.READY,
        idle_expires_at=int(time.time()) + 9999))
    expired = await repo.expired_sessions()
    ids = [s.session_id for s in expired]
    assert f"bs-sweep-{_RUN}" in ids
    assert f"bs-live-{_RUN}" not in ids


# ── Execution steps (§10.5/§10.6) ─────────────────────────────────────

def _step(**overrides):
    defaults = dict(
        step_id=f"step-{_RUN}-1", logical_action_id=f"la-{_RUN}",
        run_id=f"run-{_RUN}", task_id="task-1", tenant_id="u1",
        sequence=0)
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_step_create_and_cas_transition_on_firestore(fs):
    """§10.6 on Firestore: happy-path CAS transition bumps the version;
    a stale expected_version conflicts; an illegal transition is refused."""
    from backend.repositories.firestore.execution_step_repository import (
        FirestoreExecutionStepRepository)
    from backend.repositories.execution_step_repository import (
        StepState, StepTransitionError)
    repo = FirestoreExecutionStepRepository()
    created = await repo.create_step(_step())
    assert created["state"] == StepState.OBSERVING.value
    assert created["state_version"] == 1

    moved = await repo.transition(created["step_id"], StepState.PROPOSED,
                                  actor="agent")
    assert moved["state"] == StepState.PROPOSED.value
    assert moved["state_version"] == 2

    with pytest.raises(StepTransitionError):
        # agent may not VALIDATE
        await repo.transition(created["step_id"], StepState.VALIDATING,
                              actor="agent")
    with pytest.raises(StepTransitionError):
        await repo.transition(created["step_id"], StepState.VALIDATING,
                              actor="system", expected_version=99)

    moved2 = await repo.transition(created["step_id"], StepState.VALIDATING,
                                   actor="system", expected_version=2)
    assert moved2["state_version"] == 3


@pytest.mark.asyncio
async def test_step_dispatch_uncertainty_and_queries_on_firestore(fs):
    """§10.6 on Firestore: DISPATCHING → OUTCOME_UNCERTAIN is allowed
    (lost response); run/task queries sort by sequence."""
    from backend.repositories.firestore.execution_step_repository import (
        FirestoreExecutionStepRepository)
    from backend.repositories.execution_step_repository import StepState
    repo = FirestoreExecutionStepRepository()
    s1 = await repo.create_step(_step(step_id=f"step-{_RUN}-a", sequence=1))
    await repo.transition(s1["step_id"], StepState.PROPOSED, actor="agent")
    await repo.transition(s1["step_id"], StepState.VALIDATING, actor="system")
    await repo.transition(s1["step_id"], StepState.READY, actor="system")
    await repo.transition(s1["step_id"], StepState.DISPATCHING, actor="system")
    uncertain = await repo.transition(s1["step_id"],
                                      StepState.OUTCOME_UNCERTAIN, actor="system")
    assert uncertain["state"] == StepState.OUTCOME_UNCERTAIN.value
    with pytest.raises(Exception):
        await repo.transition(s1["step_id"], StepState.VERIFYING,
                              actor="system")

    await repo.create_step(_step(step_id=f"step-{_RUN}-b", sequence=0))
    steps = await repo.steps_for_run(f"run-{_RUN}")
    assert [s["sequence"] for s in steps] == [0, 1], "client-side sequence sort"


# ── Recovery episodes (§11.3/§11.7/§11.8/§11.9) ───────────────────────

def _episode(**overrides):
    defaults = dict(
        episode_id=f"ep-{_RUN}", tenant_id="u1", actor_id="system",
        run_id=f"run-{_RUN}", task_id="task-1", failure_code="TIMEOUT",
        failure_category="temporary_read_failure", goal_digest="gd-1",
        acceptance_criteria=[{"kind": "non_empty", "value": "reply"}])
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
async def test_episode_claim_and_generation_on_firestore(fs):
    """§11.7 on Firestore: a live lease blocks a different worker; an
    expired lease (or the current worker's own claim) wins and ADVANCES
    the ownership generation."""
    from backend.repositories.firestore.recovery_episode_repository import (
        FirestoreRecoveryEpisodeRepository)
    repo = FirestoreRecoveryEpisodeRepository()
    eid = f"ep-claim-{_RUN}"
    await repo.create_episode(_episode(episode_id=eid))

    first = await repo.claim_episode(eid, "worker-A", lease_seconds=120)
    assert first is not None and first["owner_worker_id"] == "worker-A"
    gen0 = first["owner_generation"]

    blocked = await repo.claim_episode(eid, "worker-B", lease_seconds=120)
    assert blocked is None, "a live lease blocks a different worker"

    resumed = await repo.claim_episode(eid, "worker-A", lease_seconds=120)
    assert resumed is not None and resumed["owner_generation"] > gen0


@pytest.mark.asyncio
async def test_episode_transition_and_counters_on_firestore(fs):
    from backend.repositories.firestore.recovery_episode_repository import (
        FirestoreRecoveryEpisodeRepository)
    from backend.repositories.recovery_episode_repository import (
        RecoveryState, RecoveryTransitionError)
    repo = FirestoreRecoveryEpisodeRepository()
    eid = f"ep-tr-{_RUN}"
    await repo.create_episode(_episode(episode_id=eid))

    moved = await repo.transition(eid, RecoveryState.CLASSIFIED, actor="system")
    assert moved["state"] == RecoveryState.CLASSIFIED.value
    assert moved["state_version"] == 2

    with pytest.raises(RecoveryTransitionError):
        await repo.transition(eid, RecoveryState.PROPOSED, actor="system",
                              expected_version=99)

    await repo.increment_counters(eid, model_calls=2, tool_calls=1,
                                  attempt_count=1)
    ep = await repo.get_episode(eid)
    assert ep["model_calls"] == 2 and ep["attempt_count"] == 1


@pytest.mark.asyncio
async def test_episode_abandoned_sweep_on_firestore(fs):
    from backend.repositories.firestore.recovery_episode_repository import (
        FirestoreRecoveryEpisodeRepository)
    repo = FirestoreRecoveryEpisodeRepository()
    await repo.create_episode(_episode(
        episode_id=f"ep-ab-{_RUN}", lease_expires_at=1))   # expired
    await repo.create_episode(_episode(
        episode_id=f"ep-live-{_RUN}",
        lease_expires_at=int(time.time()) + 9999))
    abandoned = await repo.abandoned_episodes(lease_grace_seconds=0)
    ids = [e["episode_id"] for e in abandoned]
    assert f"ep-ab-{_RUN}" in ids
    assert f"ep-live-{_RUN}" not in ids


@pytest.mark.asyncio
async def test_episode_for_task_skips_terminal_on_firestore(fs):
    from backend.repositories.firestore.recovery_episode_repository import (
        FirestoreRecoveryEpisodeRepository)
    from backend.repositories.recovery_episode_repository import RecoveryState
    repo = FirestoreRecoveryEpisodeRepository()
    rid = f"run-term-{_RUN}"
    done = await repo.create_episode(_episode(episode_id=f"ep-done-{_RUN}",
                                              run_id=rid))
    await repo.transition(done["episode_id"], RecoveryState.CLASSIFIED,
                          actor="system")
    await repo.transition(done["episode_id"], RecoveryState.ESCALATED,
                          actor="system")
    assert await repo.get_episode_for_task(rid, "task-1") is None, \
        "terminal episodes are not resumable for the task"


# ── Effect ledger (Phase 5 §5.2/§5.5/§5.9/§5.11) ──────────────────────

def _effect(**overrides):
    from backend.models.effect import Effect, EffectState, new_effect_id
    from backend.repositories.effect_repository import request_digest
    digest = request_digest("u1", "email.send", {"r": [f"a-{_RUN}@x.io"]})
    defaults = dict(
        effect_id=new_effect_id(), operation_name="email.send",
        owner_user_id="u1", run_id=f"run-fx-{_RUN}", task_id="task-1",
        request_digest=digest, target=f"a-{_RUN}@x.io",
        state=EffectState.PREPARED)
    defaults.update(overrides)
    return Effect(**defaults)


@pytest.mark.asyncio
async def test_effect_transport_retry_preserves_identity_on_firestore(fs):
    """§5.2 rule 1 on Firestore: the same semantic request returns the SAME
    effect (created=False); a new digest gets a NEW effect."""
    from backend.repositories.effect_repository import request_digest
    from backend.repositories.firestore.effect_repository import (
        FirestoreEffectRepository)
    repo = FirestoreEffectRepository()
    first, created1 = await repo.prepare_effect(_effect())
    assert created1 is True
    retry, created2 = await repo.prepare_effect(_effect())
    assert created2 is False
    assert retry.effect_id == first.effect_id

    changed, created3 = await repo.prepare_effect(_effect(
        request_digest=request_digest("u1", "email.send", {"r": ["other@x.io"]}),
        target="other@x.io"))
    assert created3 is True and changed.effect_id != first.effect_id


@pytest.mark.asyncio
async def test_effect_concurrent_duplicate_preares_resolve_to_one(fs):
    """§5.2 on Firestore: two CONCURRENT prepares with different effect ids
    and the same digest — the digest-anchor transaction serializes them to
    the SAME effect; exactly one caller sees created=True."""
    from backend.repositories.firestore.effect_repository import (
        FirestoreEffectRepository)
    repo = FirestoreEffectRepository()
    a, b = _effect(), _effect()
    assert a.effect_id != b.effect_id
    results = await asyncio.gather(repo.prepare_effect(a),
                                   repo.prepare_effect(b))
    ids = {r[0].effect_id for r in results}
    assert len(ids) == 1, f"concurrent duplicates must resolve to one effect, got {ids}"
    assert sorted(r[1] for r in results) == [False, True]


@pytest.mark.asyncio
async def test_effect_cas_transition_and_receipts_on_firestore(fs):
    """§5.11 on Firestore: version CAS (stale resolver conflicts);
    §5.9 receipts: the same evidence is not double-recorded."""
    from backend.models.effect import EffectReceipt, EffectState, EffectTransitionError
    from backend.repositories.firestore.effect_repository import (
        FirestoreEffectRepository)
    repo = FirestoreEffectRepository()
    fx, _ = await repo.prepare_effect(_effect())

    moved = await repo.transition(fx.effect_id, EffectState.DISPATCHING,
                                  actor="test")
    assert moved.state is EffectState.DISPATCHING
    with pytest.raises(EffectTransitionError):
        # durable version is 2; the stale caller's expected_version=1
        await repo.transition(fx.effect_id, EffectState.PROVIDER_PENDING,
                              actor="test", expected_version=1)

    receipt = EffectReceipt(
        effect_id=fx.effect_id, effect_attempt_id="att-1", provider="smtp",
        provider_status="smtp_accepted", response_digest="resp-digest-1",
        detail={"accepted": ["a@x.io"]})
    await repo.record_receipt(receipt)
    await repo.record_receipt(receipt)   # same evidence — ignored
    rows = await repo.receipts_for(fx.effect_id)
    assert len(rows) == 1
    assert rows[0]["provider_status"] == "smtp_accepted"


@pytest.mark.asyncio
async def test_effect_unresolved_and_run_queries_on_firestore(fs):
    from backend.models.effect import EffectState
    from backend.repositories.firestore.effect_repository import (
        FirestoreEffectRepository)
    repo = FirestoreEffectRepository()
    fx, _ = await repo.prepare_effect(_effect())
    await repo.transition(fx.effect_id, EffectState.DISPATCHING, actor="test")
    await repo.transition(fx.effect_id, EffectState.OUTCOME_UNKNOWN, actor="test")

    unresolved = await repo.unresolved_effects()
    assert fx.effect_id in [e.effect_id for e in unresolved]
    for_run = await repo.effects_for_run(f"run-fx-{_RUN}")
    assert [e.effect_id for e in for_run] == [fx.effect_id]


@pytest.mark.asyncio
async def test_effect_cancelled_digest_is_reusable_on_firestore(fs):
    """SQLite partial-index parity: after CANCELLED_BEFORE_DISPATCH the
    digest is free — a deliberate re-send gets a NEW effect."""
    from backend.models.effect import EffectState
    from backend.repositories.firestore.effect_repository import (
        FirestoreEffectRepository)
    repo = FirestoreEffectRepository()
    first, created1 = await repo.prepare_effect(_effect())
    assert created1 is True
    await repo.transition(first.effect_id, EffectState.CANCELLED_BEFORE_DISPATCH,
                          actor="operator")
    again, created2 = await repo.prepare_effect(_effect())
    assert created2 is True
    assert again.effect_id != first.effect_id


# ── Experiments (Phase 13) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_experiment_lifecycle_and_cas_on_firestore(fs):
    """§13 on Firestore: propose → evaluate → shadow → canary with CAS
    version conflicts refused; the SHARED state-machine/permission tables
    gate transitions (an operator cannot promote directly)."""
    from backend.models.experiment import ExperimentKind, ExperimentState, ExperimentThresholds
    from backend.repositories.firestore.experiment_repository import (
        FirestoreExperimentRepository)
    from backend.services.improvement_service import ImprovementService
    repo = FirestoreExperimentRepository()
    svc = ImprovementService(repo)
    exp = await svc.propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:fs-{_RUN}",
        hypothesis="h", expected_benefit="b",
        baseline_ref="v3", candidate_ref="v4",
        dataset_ref="ds", dataset={"tasks": [1, 2, 3]},
        thresholds=ExperimentThresholds(success_min=0.8),
        proposer="operator:alice")
    assert exp.state is ExperimentState.PROPOSED

    results = await svc.evaluate(
        exp.experiment_id, evaluator="operator:bob",
        run_evaluator=lambda v, d: {
            "success": 0.85, "samples": 20,
            "constraint_no_unauthorized_actions": True,
            "constraint_no_unresolved_required_effects": True,
            "constraint_no_unverified_success": True},
        dataset={"tasks": [1, 2, 3]})
    assert results["candidate"]["success"] == 0.85

    # CAS conflict: a stale expected_version cannot re-transition.
    from backend.models.experiment import ExperimentTransitionError
    fetched = await repo.get_experiment(exp.experiment_id)
    with pytest.raises(ExperimentTransitionError):
        await repo.transition(exp.experiment_id, ExperimentState.SHADOW,
                              role="activator", actor="operator:carol",
                              expected_version=99)

    await svc.start_shadow(exp.experiment_id, activator="operator:carol")
    await svc.start_canary(exp.experiment_id, activator="operator:carol",
                           cohort_size=5)
    promoted = await svc.promote(exp.experiment_id,
                                 approver="operator:dave",
                                 deployment_version=f"deploy-{_RUN}",
                                 total_population=100)
    assert promoted.state is ExperimentState.PROMOTED
    assert promoted.promoted_dataset_hash == promoted.dataset_hash
    assert promoted.deployment_version == f"deploy-{_RUN}"


@pytest.mark.asyncio
async def test_experiment_permissions_enforced_on_firestore(fs):
    """§13.4 on Firestore: the proposer cannot approve; agents cannot
    promote; the constraint veto holds regardless of average quality."""
    from backend.models.experiment import ExperimentKind, ExperimentThresholds
    from backend.repositories.firestore.experiment_repository import (
        FirestoreExperimentRepository)
    from backend.services.improvement_service import ImprovementError, ImprovementService
    repo = FirestoreExperimentRepository()
    svc = ImprovementService(repo)
    exp = await svc.propose(
        kind=ExperimentKind.POLICY, target=f"policy:routing-{_RUN}",
        hypothesis="h", expected_benefit="b",
        baseline_ref="v1", candidate_ref="v2",
        dataset_ref="ds", dataset={"tasks": ["x"]},
        thresholds=ExperimentThresholds(
            success_min=0.5, critical_constraints=("safe_outputs",)),
        proposer="operator:alice")
    await svc.evaluate(exp.experiment_id, evaluator="operator:bob",
                       run_evaluator=lambda v, d: {
                           "success": 0.99, "samples": 20,
                           "constraint_safe_outputs": v == "v1",
                           "constraint_no_unauthorized_actions": True,
                           "constraint_no_unresolved_required_effects": True,
                           "constraint_no_unverified_success": True},
                       dataset={"tasks": ["x"]})
    await svc.start_shadow(exp.experiment_id, activator="operator:carol")
    await svc.start_canary(exp.experiment_id, activator="operator:carol",
                           cohort_size=3)

    with pytest.raises(ImprovementError, match="own candidate"):
        await svc.promote(exp.experiment_id, approver="operator:alice",
                          deployment_version="v1")
    with pytest.raises(ImprovementError, match="Agents cannot"):
        await svc.promote(exp.experiment_id, approver="agent:improver",
                          deployment_version="v1")
    with pytest.raises(ImprovementError, match="Critical constraint"):
        await svc.promote(exp.experiment_id, approver="operator:dave",
                          deployment_version="v1")


@pytest.mark.asyncio
async def test_episode_creation_dedup_and_deadline_on_firestore(fs):
    """§12.5 on Firestore: concurrent duplicate creation resolves to ONE
    active episode; the deadline persists; escalation works from DETECTED."""
    import asyncio
    from backend.repositories.firestore.recovery_episode_repository import (
        FirestoreRecoveryEpisodeRepository)
    repo = FirestoreRecoveryEpisodeRepository()
    base = _episode(episode_id=f"ep-dup-a-{_RUN}")
    other = _episode(episode_id=f"ep-dup-b-{_RUN}")
    results = await asyncio.gather(repo.create_episode(base),
                                   repo.create_episode(other))
    ids = {e["episode_id"] for e in results}
    assert len(ids) == 1, "concurrent creation dedups to one active episode"


# ── Prompt versions + policy snapshots (Phase 13 §13.5) ───────────────

@pytest.mark.asyncio
async def test_prompt_versions_and_rollback_on_firestore(fs):
    """§13.5 on Firestore: immutable versions, activation pointer,
    rollback restores the PRIOR version exactly."""
    from backend.repositories.firestore.prompt_versions import (
        FirestorePromptVersionStore)
    store = FirestorePromptVersionStore()
    v1 = await store.checkout(f"p-{_RUN}", "BASE {goal}", created_by="op:a")
    assert v1["version"] == 1
    v2 = await store.checkout(f"p-{_RUN}", "CAND {goal}", created_by="agent:x")
    assert v2["version"] == 2
    await store.activate(f"p-{_RUN}", v2["version_id"], activated_by="op:b")
    assert (await store.get_active(f"p-{_RUN}"))["template"] == "CAND {goal}"
    await store.rollback_to(f"p-{_RUN}", 1, operator="op:c")
    active = await store.get_active(f"p-{_RUN}")
    assert active["template"] == "BASE {goal}"
    assert active["version"] == 1
    assert await store.get_version(f"p-{_RUN}", 2) is not None


# ── Agent task contracts (Phase 15) + routing log (Phase 14) ──────────

@pytest.mark.asyncio
async def test_agent_task_contracts_on_firestore(fs):
    """§15 on Firestore: role boundaries, duplicate dedup, CAS claim race,
    typed output validation."""
    import asyncio
    from backend.models.agent_task import AgentRole, AgentTaskError, AgentTaskState
    from backend.repositories.firestore.agent_task_repository import (
        FirestoreAgentTaskRepository)
    from backend.services.agent_contracts import cancel, complete, delegate
    repo = FirestoreAgentTaskRepository()
    with pytest.raises(AgentTaskError, match="authority boundary"):
        await delegate(parent_kind="run", parent_id=f"run-{_RUN}",
                       role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                       input_data={}, allowed_tools=["mcp.dispatch"], repo=repo)
    criteria = [{"kind": "non_empty", "value": "verdict"}]
    contract = await delegate(
        parent_kind="run", parent_id=f"run-{_RUN}", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", input_data={"evidence": {}},
        output_schema={"required": ["verdict"]},
        allowed_tools=["evidence.read"], dedup_key=f"v-{_RUN}",
        acceptance_criteria=criteria, repo=repo)
    with pytest.raises(AgentTaskError, match="already delegated"):
        # The duplicate is a RESUBMISSION of the same delegation — same
        # inputs, same dedup key.
        await delegate(
            parent_kind="run", parent_id=f"run-{_RUN}",
            role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
            input_data={"evidence": {}},
            allowed_tools=["evidence.read"], dedup_key=f"v-{_RUN}",
            acceptance_criteria=criteria, repo=repo)
    await repo.claim(contract.contract_id, "w1")
    with pytest.raises(AgentTaskError, match="output schema"):
        await complete(contract.contract_id, {"nope": 1}, actor="w1", repo=repo)
    done = await complete(contract.contract_id, {"verdict": "pass"}, actor="w1", repo=repo)
    assert done.state is AgentTaskState.COMPLETED


@pytest.mark.asyncio
async def test_routing_log_on_firestore(fs):
    """§14 on Firestore: decisions log with honest probability; feedback
    joins exactly once; missing feedback stays explicit."""
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.firestore.routing_repository import (
        FirestoreRoutingDecisionRepository, new_decision_id)
    repo = FirestoreRoutingDecisionRepository()
    await repo.log_decision(RoutingDecision(
        decision_id=new_decision_id(), run_id=f"run-rt-{_RUN}", task_id="t1",
        eligible_actions=["m-a", "m-b"], selected_action="m-a",
        selection_probability=0.5, policy_version="ucb1-v1-verified-feedback"))
    rows = await repo.decisions_for_run(f"run-rt-{_RUN}")
    assert rows[0]["reward_class"] == "missing"
    assert (await repo.attach_feedback(f"run-rt-{_RUN}", "verified", True,
                                       "eval-1")) == 1
    assert (await repo.attach_feedback(f"run-rt-{_RUN}", "rejected", False,
                                       "eval-2")) == 0, "exactly once"
    health = await repo.logging_health()
    assert health["total_decisions"] >= 1
