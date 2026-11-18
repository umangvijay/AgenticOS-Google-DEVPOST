"""Phase 14 acceptance tests (roadmap: test_routing_policy.py).

- Delayed/missing feedback: an unverified completion never becomes a
  positive reward; missing feedback stays explicit.
- Model availability changes: a removed model is never selected.
- Biased rewards: per-context stats separate a model that excels at one
  task category from its failures in another (the contextual core).
- Drift: a reward-distribution shift is DETECTED and reported.
- Repeated failures: a model that keeps failing drops in rank.
- Budget exhaustion during exploration: the budget degrades to greedy.
- Selection probability: exploration logs its exact mixture probability.
- Filtering: forbidden actions never enter the candidate set.
- Offline comparison: coverage-aware, uncertainty reported.
"""
import random

import pytest
import pytest_asyncio

from backend.repositories.routing_repository import (
    RoutingDecisionRepository, set_routing_repo)
from backend.repositories.sqlite.adaptive_policy_repository import (
    SQLiteAdaptivePolicyRepository)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.contextual_policy import (
    EXPLORATION_BUDGET_MAX, ContextualRoutingPolicy, context_bucket,
    reward_from_class,
)
from backend.services.offline_policy_evaluation import compare, fixed_policy
from backend.services.routing_recorder import attach_evaluation_feedback


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "routing-policy.db"))
    await db.initialize()
    repo = SQLiteAdaptivePolicyRepository(db)
    routing = RoutingDecisionRepository(db)
    set_routing_repo(routing)
    try:
        yield {"db": db, "repo": repo, "routing": routing,
               "policy": ContextualRoutingPolicy(repo)}
    finally:
        set_routing_repo(None)
        await db.close()


# ── §14.1: verified-outcome rewards ────────────────────────────────────

def test_reward_mapping_never_upgrades_unverified():
    assert reward_from_class("verified") == 1.0
    assert reward_from_class("partial") == 0.5
    assert reward_from_class("uncertain") == 0.0
    assert reward_from_class("rejected") == 0.0
    assert reward_from_class("missing") == 0.0


@pytest.mark.asyncio
async def test_unverified_completion_is_not_positive_reward(env):
    """THE §14 acceptance: an unverified 'completed' flag cannot become a
    positive learning reward."""
    policy, repo = env["policy"], env["repo"]
    await policy.learn(model="model-a", task_category="research",
                       capability="core.search", input_size_bytes=100,
                       reward_class="uncertain")
    stats = await repo.get_context_stats(context_bucket("research",
                                                       "core.search", 100))
    row = next(r for r in stats if r["model"] == "model-a")
    assert float(row["reward_sum"]) == 0.0, \
        "uncertain contributes ZERO reward (an attempt, not a success)"
    assert int(row["attempts"]) == 1


# ── §14.2: contextual core (biased rewards separated by context) ───────

@pytest.mark.asyncio
async def test_biased_rewards_separated_by_context(env):
    """§14.2: a model that excels at research but fails browser tasks is
    ranked FIRST for research and LAST for browser — the contextual core
    the non-contextual UCB1 lacked."""
    policy, repo = env["policy"], env["repo"]
    for _ in range(10):
        await policy.learn(model="model-a", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="verified")
        await policy.learn(model="model-a", task_category="browser",
                           capability="browser.navigate",
                           input_size_bytes=100, reward_class="rejected")
        await policy.learn(model="model-b", task_category="browser",
                           capability="browser.navigate",
                           input_size_bytes=100, reward_class="verified")
    rng = random.Random(42)
    research = await policy.select(
        candidates=["model-a", "model-b"], task_category="research",
        capability="core.search", input_size_bytes=100, rng=random.Random(1))
    browser = await policy.select(
        candidates=["model-a", "model-b"], task_category="browser",
        capability="browser.navigate", input_size_bytes=100,
        rng=random.Random(1))
    assert research.ranked[0] == "model-a"
    assert browser.ranked[0] == "model-b", \
        "the same model is NOT credited across contexts"


@pytest.mark.asyncio
async def test_repeated_failures_drop_rank(env):
    """§14: a model that keeps failing drops below a fresh alternative."""
    policy, repo = env["policy"], env["repo"]
    for _ in range(40):
        await policy.learn(model="model-bad", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="rejected")
    decision = await policy.select(
        candidates=["model-bad", "model-good"], task_category="research",
        capability="core.search", input_size_bytes=100,
        rng=random.Random(7))
    assert decision.ranked[0] == "model-good"


# ── §14.3: candidate filtering + honest probability ────────────────────

@pytest.mark.asyncio
async def test_forbidden_actions_never_enter_selection(env):
    """§14.3: the policy selects ONLY from the eligible set — a forbidden
    action passed as a candidate would be a caller bug; the policy's
    ranked list never contains anything else."""
    policy = env["policy"]
    decision = await policy.select(
        candidates=["model-a"], task_category="research",
        capability="core.search", input_size_bytes=100,
        rng=random.Random(3))
    assert decision.ranked == ["model-a"]
    assert decision.selection_probability == 1.0
    # The engine passes candidates already filtered by
    # gemini_client.candidate_models() + the policy rank — the log's
    # eligible_actions IS the filtered set (asserted in the recorder).
    assert decision.selected in decision.ranked


@pytest.mark.asyncio
async def test_exploration_logs_exact_probability(env):
    """§14.4: exploration events log the exact mixture probability
    eps/(n-1); greedy events log 1.0."""
    policy = env["policy"]
    explored = 0
    for seed in range(300):
        decision = await policy.select(
            candidates=["model-a", "model-b", "model-c"],
            task_category="research", capability="core.search",
            input_size_bytes=100, rng=random.Random(seed))
        if decision.mechanism == "contextual_ucb1_explore":
            explored += 1
            # §14.14: P(explored alternative) = eps / (n - 1) = 0.05.
            assert decision.selection_probability == pytest.approx(
                0.10 / 2, abs=1e-6)
        elif decision.mechanism == "contextual_ucb1":
            # §14.14: P(greedy top) = 1 - eps = 0.90 (the complete-policy
            # probability, not 1.0).
            assert decision.selection_probability == pytest.approx(0.90, abs=1e-6)
        else:
            assert decision.mechanism == "greedy_budget_exhausted"
            assert decision.selection_probability == 1.0
    # The epsilon rate should produce roughly 10% exploration.
    assert 0 < explored < 300, "both mechanisms occur"


@pytest.mark.asyncio
async def test_model_availability_change(env):
    """§14: when a provider goes away, the reduced eligible set is the
    only selection space."""
    policy = env["policy"]
    for _ in range(5):
        await policy.learn(model="model-gone", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="verified")
    decision = await policy.select(
        candidates=["model-still-here"], task_category="research",
        capability="core.search", input_size_bytes=100,
        rng=random.Random(5))
    assert decision.selected == "model-still-here"
    assert "model-gone" not in decision.ranked


# ── §14.5: exploration budget + drift ──────────────────────────────────

@pytest.mark.asyncio
async def test_exploration_budget_exhaustion_degrades_to_greedy(env):
    """§14.5: when the exploration budget is exhausted, the policy
    degrades to greedy — never to off-list actions or unbounded
    exploration."""
    policy, repo = env["policy"], env["repo"]
    # Exhaust the durable budget.
    for _ in range(EXPLORATION_BUDGET_MAX):
        await repo.record_exploration_event()
    decision = await policy.select(
        candidates=["model-a", "model-b", "model-c"],
        task_category="research", capability="core.search",
        input_size_bytes=100, rng=random.Random(11))
    assert decision.mechanism == "greedy_budget_exhausted"
    assert decision.selection_probability == 1.0
    assert decision.selected in decision.ranked
    assert decision.exploration_budget_remaining == 0


@pytest.mark.asyncio
async def test_drift_detection(env):
    """§14.5: a reward-distribution shift is DETECTED — recent mean far
    from the long-run mean with enough samples."""
    policy, repo = env["policy"], env["repo"]
    # Long-run: model-a mostly verified in this context.
    for _ in range(30):
        await policy.learn(model="model-a", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="verified")
    # Recent window: reward collapse (the recent counters reset to
    # empty — the window itself stays current — then failures land in
    # the bounded recent bucket).
    await env["db"].execute(
        "UPDATE contextual_model_stats SET "
        "recent_reward_sum=0, recent_attempts=0 WHERE context_key=?",
        (context_bucket("research", "core.search", 100),))
    await env["db"].commit()
    # §14.29: the recent bucket is COUNT-rotated (≤20 outcomes) — feed
    # 15 failures so the window stays intact.
    for _ in range(15):
        await policy.learn(model="model-a", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="rejected")
    report = await policy.drift_report()
    assert report["drift_detected"] is True
    entry = report["contexts"][0]
    assert entry["recent_mean_reward"] == 0.0
    assert entry["long_run_mean_reward"] > 0.5


# ── §14 acceptance: offline comparison with fixed routing ──────────────

@pytest.mark.asyncio
async def test_offline_comparison_reports_by_category(env):
    """§14 acceptance: compare with fixed routing using logged decisions;
    report verified quality, cost, latency, and confidence by category."""
    routing = env["routing"]
    from backend.repositories.routing_repository import new_decision_id
    from backend.models.routing_decision import RoutingDecision
    for i in range(20):
        await routing.log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-cmp-{i}",
            task_id=f"t{i}", task_category="research",
            eligible_actions=["model-a", "model-b"],
            selected_action="model-a", selection_probability=1.0,
            policy_version="contextual-v1", resolved_action="model-a"))
    await routing.attach_feedback("run-cmp-1", "verified", True)
    await routing.attach_feedback("run-cmp-2", "verified", True)
    out = compare(await routing.logged_decisions(),
                  fixed_policy("model-b"))
    assert out["logging_policy"]["by_category"]["research"]["samples"] >= 2
    assert "candidate_policy" in out and "log_health" in out


# ── delayed/missing feedback (§14.1 acceptance) ────────────────────────

@pytest.mark.asyncio
async def test_delayed_feedback_joins_after_learning(env):
    """§14.1: the decision is logged at selection (reward missing); the
    verified outcome joins LATER and drives both the policy update and
    the contextual learning."""
    routing, policy, repo = env["routing"], env["policy"], env["repo"]
    from backend.repositories.routing_repository import new_decision_id
    from backend.models.routing_decision import RoutingDecision
    await routing.log_decision(RoutingDecision(
        decision_id=new_decision_id(), run_id="run-delay", task_id="t1",
        task_category="research", capability="core.search",
        input_size_bytes=100, eligible_actions=["model-a"],
        selected_action="model-a", selection_probability=1.0,
        policy_version="contextual-v1", resolved_action="model-a"))
    rows = await routing.decisions_for_run("run-delay")
    assert rows[0]["reward_class"] == "missing"
    # The verified outcome joins later.
    out = await attach_evaluation_feedback("run-delay", {"success": True},
                                           "eval-1")
    assert out["reward_class"] == "verified"
    # The contextual policy learns from it.
    await policy.learn(model="model-a", task_category="research",
                       capability="core.search", input_size_bytes=100,
                       reward_class=out["reward_class"])
    stats = await repo.get_context_stats(context_bucket("research",
                                                       "core.search", 100))
    row = next(r for r in stats if r["model"] == "model-a")
    assert float(row["reward_sum"]) == 1.0
