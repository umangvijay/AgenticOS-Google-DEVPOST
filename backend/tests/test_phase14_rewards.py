"""Phase 14 §14.18: durable reward events + atomic consumption.

The acceptance rows for findings C and D:

- C (idempotent consumption): the learner's stats update and the
  consumption mark commit in ONE transaction — a crash before the
  commit re-applies on retry (no loss), a crash after it applies
  nothing (no duplication).
- D (durable rewards): rewards are events with unique identity
  (decision, evaluation, reward_version) that INDEPENDENT learners
  consume — the global leaderboard and the contextual bandit each
  consume the same event exactly once, through separate columns.
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "rewards.db"))
    await db.initialize()
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, set_routing_repo)
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    routing = RoutingDecisionRepository(db)
    stats = SQLiteAdaptivePolicyRepository(db)
    set_routing_repo(routing)
    try:
        yield {"db": db, "routing": routing, "stats": stats}
    finally:
        set_routing_repo(None)
        await db.close()


async def _decision(env, run_id="run-rw", selected="model-a",
                    evaluation_id=None):
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    d = RoutingDecision(
        decision_id=new_decision_id(), run_id=run_id, task_id="t1",
        task_category="research", capability="core.search",
        input_size_bytes=100, eligible_actions=[selected],
        selected_action=selected, selection_probability=1.0,
        policy_version="contextual-v1", resolved_action=selected)
    await env["routing"].log_decision(d)
    return d


# ── §14.18/D: durable reward events with unique identity ───────────────

@pytest.mark.asyncio
async def test_reward_event_creation_is_idempotent_on_identity(env):
    """The same (decision, evaluation, version) identity returns the SAME
    event — a retried feedback join cannot mint a duplicate reward."""
    d = await _decision(env)
    first = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e1",
        reward_class="verified", reward_value=1.0, model="model-a",
        context_key="abc123", latency_ms=250.0)
    second = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e1",
        reward_class="verified", reward_value=1.0, model="model-a",
        context_key="abc123", latency_ms=250.0)
    assert first and second and first == second
    events = await env["routing"].unconsumed_reward_events("contextual")
    assert len(events) == 1, "exactly one durable event for the identity"


@pytest.mark.asyncio
async def test_reward_events_are_immutable_records(env):
    """A reward event persists what happened: class, value, model,
    context, latency — the raw material every learner replays."""
    d = await _decision(env)
    await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e2",
        reward_class="rejected", reward_value=0.0, model="model-a",
        context_key="abc123", latency_ms=900.0)
    events = await env["routing"].unconsumed_reward_events("global")
    ev = events[0]
    assert ev["reward_class"] == "rejected"
    assert float(ev["reward_value"]) == 0.0
    assert ev["model"] == "model-a"
    assert ev["context_key"] == "abc123"
    assert float(ev["latency_ms"]) == 900.0


# ── §14.18/C: atomic consumption (no loss, no duplication) ────────────

@pytest.mark.asyncio
async def test_consumption_applies_stats_and_marks_in_one_transaction(env):
    d = await _decision(env)
    event_id = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e3",
        reward_class="verified", reward_value=1.0, model="model-a",
        context_key="ck1", latency_ms=120.0)
    applied = await env["stats"].consume_reward_event(
        event_id, consumer="contextual")
    assert applied is True
    stats = await env["stats"].get_context_stats("ck1")
    assert len(stats) == 1 and stats[0]["model"] == "model-a"
    assert float(stats[0]["reward_sum"]) == 1.0
    # The consumption mark is durable.
    events = await env["routing"].unconsumed_reward_events("contextual")
    assert events == [], "the event is consumed"


@pytest.mark.asyncio
async def test_double_consumption_applies_nothing(env):
    """The crash-after-commit window: a retry's consumption is a no-op —
    the CAS on consumed=0 fails and the stats update never re-runs."""
    d = await _decision(env)
    event_id = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e4",
        reward_class="verified", reward_value=1.0, model="model-a",
        context_key="ck2", latency_ms=100.0)
    assert await env["stats"].consume_reward_event(
        event_id, consumer="contextual") is True
    assert await env["stats"].consume_reward_event(
        event_id, consumer="contextual") is False
    stats = await env["stats"].get_context_stats("ck2")
    assert len(stats) == 1
    assert int(stats[0]["attempts"]) == 1, "attempt counted exactly once"
    assert float(stats[0]["reward_sum"]) == 1.0


@pytest.mark.asyncio
async def test_unknown_event_is_not_a_crash(env):
    assert await env["stats"].consume_reward_event(
        "rew-nonexistent", consumer="contextual") is False


# ── §14.18/D: independent learners consume the same event once each ────

@pytest.mark.asyncio
async def test_global_and_contextual_consume_independently(env):
    """The same reward event feeds BOTH learners exactly once each — the
    global leaderboard and the per-context bandit keep their own
    consumption marks (separate columns)."""
    d = await _decision(env)
    event_id = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e5",
        reward_class="verified", reward_value=1.0, model="model-a",
        context_key="ck3", latency_ms=300.0)
    assert await env["stats"].consume_reward_event(
        event_id, consumer="global") is True
    assert await env["stats"].consume_reward_event(
        event_id, consumer="contextual") is True
    # Global stats learned.
    global_stats = await env["stats"].get_stats()
    g = next(s for s in global_stats if s["model"] == "model-a")
    assert int(g["successes"]) == 1
    # Contextual stats learned independently.
    ctx = await env["stats"].get_context_stats("ck3")
    assert len(ctx) == 1 and float(ctx[0]["reward_sum"]) == 1.0
    # Neither consumer can consume again.
    assert await env["routing"].unconsumed_reward_events("global") == []
    assert await env["routing"].unconsumed_reward_events("contextual") == []


# ── The recorder publishes events and learners consume atomically ──────

@pytest.mark.asyncio
async def test_feedback_join_publishes_reward_events(env):
    """attach_evaluation_feedback publishes a durable event per rewarded
    decision — with the context key the contextual learner uses."""
    from backend.services.contextual_policy import context_bucket
    from backend.services.routing_recorder import attach_evaluation_feedback
    d = await _decision(env, run_id="run-pub", selected="model-b")
    out = await attach_evaluation_feedback("run-pub", {"success": True},
                                           "eval-pub")
    assert out["policy_applied"] == 1
    assert len(out["reward_events"]) == 1
    events = await env["routing"].unconsumed_reward_events("contextual")
    ctx_events = [e for e in events if e["context_key"] ==
                  context_bucket("research", "core.search", 100)]
    assert len(ctx_events) == 1, \
        "the event carries the context key the bandit scores with"
    assert ctx_events[0]["model"] == "model-b"


@pytest.mark.asyncio
async def test_retried_join_never_duplicates_learning(env):
    """End-to-end idempotency: the SAME evaluation joined twice produces
    one event, one application — across BOTH learners."""
    from backend.services.routing_recorder import attach_evaluation_feedback
    await _decision(env, run_id="run-idem2")
    first = await attach_evaluation_feedback("run-idem2", {"success": True},
                                             "eval-idem")
    second = await attach_evaluation_feedback("run-idem2", {"success": True},
                                              "eval-idem")
    assert first["policy_applied"] == 1
    assert second["policy_applied"] == 0, "the retry has nothing to apply"
    all_events = (await env["routing"].unconsumed_reward_events("contextual")
                  + await env["routing"].unconsumed_reward_events("global"))
    assert len(first["reward_events"]) == 1
    # Consume from both learners repeatedly — stats move exactly once.
    for _ in range(2):
        await env["stats"].consume_reward_event(
            first["reward_events"][0], consumer="global")
        await env["stats"].consume_reward_event(
            first["reward_events"][0], consumer="contextual")
    g = next(s for s in await env["stats"].get_stats())
    assert int(g["attempts"]) == 1 and int(g["successes"]) == 1


@pytest.mark.asyncio
async def test_fallback_lineage_holds_through_reward_events(env):
    """§13.6 through §14.18: the fallback-resolved row earns the verified
    reward; the failed initial model's row never does."""
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    from backend.services.routing_recorder import attach_evaluation_feedback
    # Initial model-a decision (no fallback) + model-b fallback decision.
    await env["routing"].log_decision(RoutingDecision(
        decision_id=new_decision_id(), run_id="run-fbx", task_id="t1",
        task_category="workflow", capability="core.exec",
        input_size_bytes=10, eligible_actions=["model-a"],
        selected_action="model-a", selection_probability=1.0,
        policy_version="pv", resolved_action="model-a"))
    await env["routing"].log_decision(RoutingDecision(
        decision_id=new_decision_id(), run_id="run-fbx", task_id="t1",
        task_category="workflow", capability="core.exec",
        input_size_bytes=10, eligible_actions=["model-b"],
        selected_action="model-b", selection_probability=1.0,
        policy_version="pv", resolved_action="model-b",
        fallback_from="model-a", latency_ms=812.0))
    out = await attach_evaluation_feedback("run-fbx", {"success": True},
                                           "eval-fbx")
    assert out["policy_applied"] == 1
    rewarded = set()
    for ev_id in out["reward_events"]:
        cursor = await env["db"].execute(
            "SELECT model FROM reward_events WHERE event_id=?", (ev_id,))
        row = await cursor.fetchone()
        if row:
            rewarded.add(row["model"])
    assert rewarded == {"model-b"}, \
        "only the RESOLVED model's decision earns the verified event"


# ── §14.18: the recorder works when the learner lacks event support ────

@pytest.mark.asyncio
async def test_legacy_learner_falls_back_to_record_path(env):
    """A learner without durable-event support (duck-typed) still learns
    through the legacy record path — and the event is still published
    for the consumers that CAN use it."""
    from backend.services.routing_recorder import attach_evaluation_feedback

    class LegacyLearner:
        def __init__(self):
            self.records = []

        async def record(self, model, success, latency_ms):
            self.records.append((model, success, latency_ms))

    learner = LegacyLearner()
    await _decision(env, run_id="run-legacy", selected="model-z")
    out = await attach_evaluation_feedback("run-legacy", {"success": True},
                                           "eval-legacy", adaptive=learner)
    assert learner.records == [("model-z", True, 0.0)]
    assert len(out["reward_events"]) == 1, \
        "the durable event exists regardless of the learner's support"


# ── §14.21/§14.22: offline comparison — supported estimator only ───────

@pytest.mark.asyncio
async def test_offline_comparison_names_its_estimator(env):
    """§14.22: the comparison result states WHAT each number is — the
    descriptive replay is never presented as a counterfactual value."""
    from backend.services.offline_policy_evaluation import (
        compare, fixed_policy)
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    for i in range(6):
        await env["routing"].log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-oe-{i}",
            task_id=f"t{i}", task_category="research",
            eligible_actions=["model-a", "model-b"],
            selected_action="model-a", selection_probability=1.0,
            policy_version="pv", resolved_action="model-a"))
    await env["routing"].attach_feedback("run-oe-0", "verified", True)
    out = compare(await env["routing"].logged_decisions(),
                  fixed_policy("model-b"))
    assert "estimator" in out
    assert out["estimator"]["counterfactual_supported"] is False, \
        "a deterministic log supports NO counterfactual claim"
    cand = out["candidate_policy"]
    assert cand["deterministic_log"] is True
    assert cand["counterfactual_supported"] is False
    assert "not a counterfactual policy value" in \
        cand["descriptive"]["note"]


@pytest.mark.asyncio
async def test_ips_uses_logged_propensities(env):
    """§14.22: with stochastic propensities in the log, the IPS term
    weights by 1/p on the agreement set and flags support honestly."""
    from backend.services.offline_policy_evaluation import (
        replay_candidate, fixed_policy)
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    # Six decisions: 3 logged with p=0.9, 3 with p=0.5; candidate agrees
    # with every logged action.
    probs = [0.9, 0.9, 0.9, 0.5, 0.5, 0.5]
    for i, p in enumerate(probs):
        d = RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-ips-{i}",
            task_id=f"t{i}", task_category="workflow",
            eligible_actions=["model-a", "model-b"],
            selected_action="model-a", selection_probability=p,
            policy_version="pv", resolved_action="model-a")
        await env["routing"].log_decision(d)
        await env["routing"].attach_feedback(f"run-ips-{i}",
                                             "verified", True)
    out = replay_candidate(await env["routing"].logged_decisions(),
                           fixed_policy("model-a"))
    # Full agreement: IPS = (1/N) Σ r_i/p_i = (3/0.9 + 3/0.5)/6 — the
    # honest finite-sample value, > 1.0 because the low-propensity
    # entries carry heavy IPS weights on this small sample.
    expected = round(sum(1.0 / p for p in probs) / len(probs), 4)
    assert out["ips_scored_decisions"] == 6
    assert out["ips_agreement_decisions"] == 6
    assert out["deterministic_log"] is False
    assert out["ips_value"] == expected
    assert out["counterfactual_supported"] is False, \
        "6 scored decisions < MIN_SUPPORT_PER_ARM — honestly unsupported"


@pytest.mark.asyncio
async def test_unsupported_candidate_picks_stay_unsupported(env):
    """§14.5: a candidate whose pick lacks verified support shows the
    number as unsupported — never a silently averaged estimate."""
    from backend.services.offline_policy_evaluation import (
        replay_candidate, fixed_policy)
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    # model-b NEVER appears in the log; the fixed candidate picks it.
    for i in range(4):
        await env["routing"].log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-us-{i}",
            task_id=f"t{i}", task_category="workflow",
            eligible_actions=["model-a", "model-b"],
            selected_action="model-a", selection_probability=1.0,
            policy_version="pv", resolved_action="model-a"))
        # model-a's verified outcomes join the log; model-b has NONE.
        await env["routing"].attach_feedback(f"run-us-{i}",
                                             "verified", True)
    out = replay_candidate(await env["routing"].logged_decisions(),
                           fixed_policy("model-b"))
    assert out["descriptive"]["estimated_value"] is None
    assert out["descriptive"]["unsupported_picks"] == 4
    assert out["confidence"] == "low"


# ── §14.20: cost travels with the reward and scores as a tie-breaker ───

@pytest.mark.asyncio
async def test_cost_units_travel_and_land_in_contextual_stats(env):
    """§14.20: the reward event carries the measured cost; the
    contextual learner records its EWMA next to latency."""
    d = await _decision(env, run_id="run-cost", selected="model-c")
    event_id = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e-cost",
        reward_class="verified", reward_value=1.0, model="model-c",
        context_key="ck-cost", latency_ms=200.0, cost_units=12.0)
    assert await env["stats"].consume_reward_event(
        event_id, consumer="contextual") is True
    stats = await env["stats"].get_context_stats("ck-cost")
    row = next(r for r in stats if r["model"] == "model-c")
    assert float(row["ewma_cost_units"]) == 12.0


@pytest.mark.asyncio
async def test_cost_is_never_a_bonus_without_measurement(env):
    """§14.20: a model with NO measured cost gains nothing from the cost
    term — a missing measurement is never a scoring bonus."""
    from backend.services.contextual_policy import ContextualRoutingPolicy
    policy = ContextualRoutingPolicy(env["stats"])
    # model-x: 40 failures; model-y: never attempted (unknown bonus 0.5).
    for _ in range(40):
        await policy.learn(model="model-x", task_category="research",
                           capability="core.search", input_size_bytes=5,
                           reward_class="rejected")
    decision = await policy.select(
        candidates=["model-x", "model-y"], task_category="research",
        capability="core.search", input_size_bytes=5,
        rng=__import__("random").Random(1))
    assert decision.ranked[0] == "model-y", \
        "the unmeasured model still outranks a consistent failure"


# ── §14.21: malformed reward events are quarantined, never applied ─────

@pytest.mark.asyncio
async def test_malformed_reward_event_is_quarantined(env):
    """A reward event with an invalid value is marked consumed WITH a
    quarantine reason — it cannot poison the learner loop or reprocess
    forever, and it never becomes learning."""
    d = await _decision(env, run_id="run-q", selected="model-q")
    event_id = await env["routing"].create_reward_event(
        decision_id=d.decision_id, evaluation_id="e-q",
        reward_class="verified", reward_value=7.5, model="model-q",
        context_key="ck-q")
    applied = await env["stats"].consume_reward_event(
        event_id, consumer="contextual")
    assert applied is True, "the event is consumed (quarantined)"
    cursor = await env["db"].execute(
        "SELECT quarantined, quarantine_reason FROM reward_events "
        "WHERE event_id=?", (event_id,))
    row = await cursor.fetchone()
    assert int(row["quarantined"]) == 1
    assert "malformed" in row["quarantine_reason"]
    assert await env["stats"].get_context_stats("ck-q") == [], \
        "quarantined events never become learning"
    # The loop is clean: nothing unconsumed remains.
    assert await env["routing"].unconsumed_reward_events("contextual") == []
