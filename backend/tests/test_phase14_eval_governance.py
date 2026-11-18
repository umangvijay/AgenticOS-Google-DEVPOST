"""Phase 14 §14.23–§14.25: baselines, uncertainty, and policy artifacts.

- 14.23: the acceptance comparison — logging policy's realized value vs
  a FIXED baseline, per task category, on the same held-out log.
- 14.24: uncertainty travels with every estimate (CI95 on the category
  means AND on the IPS aggregate); support gates what may be claimed.
- 14.25: the policy ARTIFACT — the full learned state (global + per-
  context) captured and restored EXACTLY (rollback §13.5).
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "evalgov.db"))
    await db.initialize()
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, set_routing_repo)
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    from backend.services.prompt_versions import PolicySnapshotStore
    routing = RoutingDecisionRepository(db)
    stats = SQLiteAdaptivePolicyRepository(db)
    set_routing_repo(routing)
    try:
        yield {"db": db, "routing": routing, "stats": stats,
               "snapshots": PolicySnapshotStore(db)}
    finally:
        set_routing_repo(None)
        await db.close()


async def _seed(env, category, action, verdict, n=1, p=1.0):
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    for i in range(n):
        await env["routing"].log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-{category}-{action}-{verdict}-{i}",
            task_id=f"t{i}", task_category=category,
            eligible_actions=[action], selected_action=action,
            selection_probability=p, policy_version="pv",
            resolved_action=action))
        await env["routing"].attach_feedback(
            f"run-{category}-{action}-{verdict}-{i}", verdict,
            verdict == "verified")


# ── §14.23: the fixed-baseline comparison ──────────────────────────────

@pytest.mark.asyncio
async def test_fixed_baseline_comparison_by_category(env):
    from backend.services.offline_policy_evaluation import (
        compare, fixed_policy)
    await _seed(env, "research", "model-a", "verified", n=3)
    await _seed(env, "browser", "model-a", "rejected", n=2)
    out = compare(await env["routing"].logged_decisions(),
                  fixed_policy("model-a"))
    lp = out["logging_policy"]
    # The logging policy's realized value, per category, on the same log.
    assert lp["by_category"]["research"]["value"] == 1.0
    assert lp["by_category"]["browser"]["value"] == 0.0
    assert "candidate_policy" in out and "log_health" in out
    assert out["log_health"]["decisions"] == 5
    assert out["log_health"]["missing_feedback"] == 0


# ── §14.24: uncertainty on every estimate ──────────────────────────────

@pytest.mark.asyncio
async def test_category_means_carry_ci95(env):
    out = await env["routing"].summary_by_category()
    # No data yet: the summary exists with explicit empties.
    assert out["categories"] == {}
    await _seed(env, "research", "model-a", "verified", n=4)
    await _seed(env, "research", "model-a", "rejected", n=4)
    out = await env["routing"].summary_by_category()
    cat = out["categories"]["research"]
    assert cat["verified_quality"] == 0.5
    assert 0.0 <= cat["ci95_low"] <= 0.5 <= cat["ci95_high"] <= 1.0


@pytest.mark.asyncio
async def test_ips_estimate_carries_ci_and_support_flag(env):
    from backend.services.offline_policy_evaluation import (
        replay_candidate, fixed_policy)
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    # Stochastic propensities with full agreement: 12 scored decisions.
    for i in range(12):
        await env["routing"].log_decision(RoutingDecision(
            decision_id=new_decision_id(), run_id=f"run-ips-{i}",
            task_id=f"t{i}", task_category="workflow",
            eligible_actions=["m-a", "m-b"],
            selected_action="m-a", selection_probability=0.8,
            policy_version="pv", resolved_action="m-a"))
        await env["routing"].attach_feedback(f"run-ips-{i}",
                                             "verified", True)
    out = replay_candidate(await env["routing"].logged_decisions(),
                           fixed_policy("m-a"))
    assert out["ips_value"] is not None
    assert out["ips_ci95_low"] is not None
    assert out["ips_ci95_low"] <= out["ips_value"] <= out["ips_ci95_high"]
    assert out["ips_sufficient_support"] is True, \
        "12 stochastic scored decisions clear the support bar"


@pytest.mark.asyncio
async def test_deterministic_log_is_flagged_insufficient_for_counterfactuals(env):
    from backend.services.offline_policy_evaluation import (
        replay_candidate, fixed_policy)
    await _seed(env, "workflow", "m-a", "verified", n=12)
    out = replay_candidate(await env["routing"].logged_decisions(),
                           fixed_policy("m-a"))
    assert out["deterministic_log"] is True
    assert out["ips_sufficient_support"] is False, \
        "a deterministic log supports NO counterfactual claim"


# ── §14.25: the policy artifact — exact capture and restore ────────────

@pytest.mark.asyncio
async def test_policy_artifact_restores_the_learned_state_exactly(env):
    from backend.services.prompt_versions import (
        capture_policy_artifact, restore_policy_artifact)
    from backend.services.contextual_policy import ContextualRoutingPolicy
    policy = ContextualRoutingPolicy(env["stats"])
    # Learn some global and contextual state.
    await env["stats"].record_outcome("m-a", True, 150.0)
    await env["stats"].record_outcome("m-a", False, 250.0)
    for _ in range(3):
        await policy.learn(model="m-a", task_category="research",
                           capability="core.search", input_size_bytes=100,
                           reward_class="verified", latency_ms=120.0)
    before_global = {r["model"]: r for r in await env["stats"].get_stats()}
    before_ctx = await env["stats"].get_context_stats(
        __import__("backend.services.contextual_policy",
                   fromlist=["context_bucket"]).context_bucket(
                       "research", "core.search", 100))
    # Capture the artifact.
    artifact = await capture_policy_artifact(env["stats"], env["snapshots"],
                                             created_by="test")
    assert artifact["snapshot_id"]
    # Mutate the live state.
    await env["stats"].record_outcome("m-z", True, 10.0)
    await policy.learn(model="m-a", task_category="research",
                       capability="core.search", input_size_bytes=100,
                       reward_class="rejected")
    # Restore EXACTLY.
    restored = await restore_policy_artifact(
        env["stats"], await env["snapshots"].latest())
    assert restored == {"global": 1, "context": 1}
    after_global = {r["model"]: r for r in await env["stats"].get_stats()}
    assert set(after_global) == set(before_global), \
        "the post-restore mutation is gone; the artifact is the state"
    assert int(after_global["m-a"]["attempts"]) == \
        int(before_global["m-a"]["attempts"])
    after_ctx = await env["stats"].get_context_stats(
        __import__("backend.services.contextual_policy",
                   fromlist=["context_bucket"]).context_bucket(
                       "research", "core.search", 100))
    assert float(after_ctx[0]["reward_sum"]) == \
        float(before_ctx[0]["reward_sum"])
    assert int(after_ctx[0]["attempts"]) == int(before_ctx[0]["attempts"])
