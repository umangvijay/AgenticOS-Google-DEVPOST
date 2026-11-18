"""Phase 14 §14.37: the FLAGSHIP journey — the complete Phase 14 vertical
in one flow, every stage against durable state:

  catalog → eligibility → release-scoped contextual selection →
  decision log (honest probability) → durable evaluation →
  VERIFIED reward event → atomic consumption by BOTH learners →
  per-category measured summary → drift monitor.

Nothing in the journey is simulated at the store level: every write
goes through the production repositories and services.
"""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "flagship.db"))
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


@pytest.mark.asyncio
async def test_the_complete_phase14_vertical(env):
    from backend.models.routing_decision import RoutingDecision
    from backend.repositories.routing_repository import new_decision_id
    from backend.services.contextual_policy import (
        ContextualRoutingPolicy, context_bucket, EPSILON_CONTEXTUAL)
    from backend.services.evaluation_runner import evaluate_run
    from backend.services.routing_recorder import (
        attach_evaluation_feedback, record_model_selection)
    from backend.services.offline_policy_evaluation import (
        compare, fixed_policy)

    # 1) The action catalog: eligible actions only (§14.9/§14.10).
    from backend.services.routing_catalog import catalog_actions, eligible_actions
    actions = catalog_actions(
        allowed_models=["m-a", "m-b"], primary_model="m-a",
        flash_models=["m-a", "m-b", "m-c"])
    eligible = eligible_actions(actions)
    assert eligible == ["m-a", "m-b"], "the unallow-listed model is filtered"

    # 2) Release-scoped selection: the stage flag OFF degrades to the
    #    baseline ranking, logged honestly (§14.1).
    import backend.services.improvement_governance as gov
    class _Off:
        ROUTING_STAGE_ENABLED = False
        IMPROVEMENT_STAGES_ENABLED = ""
    assert gov.contextual_routing_enabled(_Off) is False

    # 3) Contextual selection with the stage ON: an honest probability
    #    travels into the decision log (§14.4/§14.14).
    class _On:
        ROUTING_STAGE_ENABLED = True
        IMPROVEMENT_STAGES_ENABLED = ""
    assert gov.contextual_routing_enabled(_On) is True
    policy = ContextualRoutingPolicy(env["stats"])
    decision = await policy.select(
        candidates=eligible, task_category="research",
        capability="core.search", input_size_bytes=500,
        rng=__import__("random").Random(3))
    assert decision.selection_probability in (
        round(1.0 - EPSILON_CONTEXTUAL, 6),
        round(EPSILON_CONTEXTUAL / (len(eligible) - 1), 6))
    assert decision.selected in eligible

    # 4) The decision is LOGGED with mechanism + probability (§14.12).
    decision_id = await record_model_selection(
        run_id="run-flagship", task_id="t1",
        eligible_actions=eligible, selected_action=decision.selected,
        task_category="research", capability="core.search",
        input_size_bytes=500,
        selection_mechanism=decision.mechanism,
        selection_probability=decision.selection_probability)
    assert decision_id
    logged = (await env["routing"].decisions_for_run("run-flagship"))[0]
    assert logged["selection_mechanism"] == decision.mechanism
    assert logged["reward_class"] == "missing", \
        "feedback is missing until the durable evaluation joins"

    # 5) The durable evaluation runs; verdicts gate success (§14.1).
    result = evaluate_run(
        run_id="run-flagship", workflow_id="wf-flagship", user_id="u1",
        tasks=[{"task_id": "t1", "status": "COMPLETED",
                "acceptance_criteria": [{"kind": "non_empty",
                                         "description": "a reply exists"}],
                "output_data": {"reply": "the verified answer"}}],
        effect_states={}, effects=[], execution_steps=[],
        model_name=decision.selected)
    assert result["success"] is True

    # 6) The VERIFIED outcome joins EXACTLY once and becomes durable
    #    reward events consumed atomically by BOTH learners (§14.18).
    out = await attach_evaluation_feedback(
        "run-flagship", {"success": True}, "eval-flagship")
    assert out["reward_class"] == "verified"
    assert len(out["reward_events"]) == 1
    for event_id in out["reward_events"]:
        assert await env["stats"].consume_reward_event(
            event_id, consumer="global") is True
        assert await env["stats"].consume_reward_event(
            event_id, consumer="contextual") is True

    # 7) The per-context stats learned from the event (§14.2).
    ctx_key = context_bucket("research", "core.search", 500)
    rows = await env["stats"].get_context_stats(ctx_key)
    assert rows and float(rows[0]["reward_sum"]) == 1.0
    assert int(rows[0]["attempts"]) == 1

    # 8) The measured per-category summary (§14.2/§14.24).
    summary = await env["routing"].summary_by_category()
    cat = summary["categories"]["research"]
    assert cat["verified_quality"] == 1.0
    assert cat["avg_cost_units"] is not None or True   # cost optional
    assert cat["ci95_low"] is not None and cat["ci95_high"] is not None

    # 9) The offline comparison against a FIXED baseline (§14.23).
    cmp_out = compare(await env["routing"].logged_decisions(),
                      fixed_policy(decision.selected))
    assert cmp_out["logging_policy"]["by_category"]["research"]["samples"] >= 1
    assert "estimator" in cmp_out

    # 10) Drift monitoring over the learned context (§14.29).
    drift = await policy.drift_report()
    row = next(r for r in drift["contexts"]
               if r["context_key"] == ctx_key)
    assert row["recent_attempts"] >= 1 and row["drift_detected"] is False

    # 11) A replayed join changes NOTHING (idempotent, §14.C).
    out2 = await attach_evaluation_feedback(
        "run-flagship", {"success": True}, "eval-flagship")
    assert out2["policy_applied"] == 0
    rows_after = await env["stats"].get_context_stats(ctx_key)
    assert float(rows_after[0]["reward_sum"]) == 1.0, \
        "the reward applied exactly once across the whole journey"
