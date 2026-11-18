"""Phase 15 §15.22–§15.24: attribution to the right specialist/attempt,
specialization evaluation vs the baseline, and learned specialist
selection through the controlled Phase 14 path."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "p15d.db"))
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


# ── §15.22: attribution ────────────────────────────────────────────────

def test_failed_builder_gets_no_credit_from_human_resolution():
    from backend.services.specialist_attribution import (
        AttemptRecord, credit_attempt, human_supplied_resolution)
    failed = AttemptRecord(
        attempt_id="att-1", specialist_role="connector_validator",
        specialist_version="v1", decision_id="rd-1", outcome_class="rejected")
    assert credit_attempt(failed, reward_value=1.0)["reward"] == 0.0
    res = human_supplied_resolution(failed_attempt_id="att-1",
                                    resolver="user:alice")
    assert res["credit_to_failed_attempt"] == 0.0
    assert res["policy_update"] is False, \
        "a human-supplied resolution never retroactively credits the " \
        "failed specialist"


def test_credit_is_per_attempt_not_per_workflow():
    from backend.services.specialist_attribution import (
        AttemptRecord, credit_attempt, branch_independence)
    good = AttemptRecord(attempt_id="att-2", specialist_role="outcome_verifier",
                         specialist_version="v1", decision_id="rd-2",
                         outcome_class="verified")
    bad = AttemptRecord(attempt_id="att-3", specialist_role="recovery_analyst",
                        specialist_version="v1", decision_id="rd-3",
                        outcome_class="rejected")
    assert credit_attempt(good, reward_value=1.0)["reward"] == 1.0
    assert credit_attempt(bad, reward_value=1.0)["reward"] == 0.0
    branches = branch_independence(branches=[
        {"branch_id": "b1", "verdict": "verified"},
        {"branch_id": "b2", "verdict": "failed"}])
    verdicts = {b["branch_id"]: b["verdict"] for b in branches["branches"]}
    assert verdicts == {"b1": "verified", "b2": "failed"}, \
        "the independent branch's success is not downgraded"


def test_uncertain_outcomes_are_tracked_not_rewarded():
    from backend.services.specialist_attribution import (
        AttemptRecord, credit_attempt)
    a = AttemptRecord(attempt_id="att-4", specialist_role="execution",
                      specialist_version="v1", decision_id="rd-4",
                      outcome_class="uncertain")
    assert credit_attempt(a, reward_value=1.0)["reward"] is None, \
        "unverified completions never become positive rewards (§12.35)"


# ── §15.23: specialization must earn its complexity ────────────────────

def test_specialization_improvement_is_measured_not_asserted():
    from backend.services.specialization_eval import evaluate_specialization
    out = evaluate_specialization(
        baseline={"verified_completion": 0.8, "total_cost": 10.0,
                  "end_to_end_latency": 120.0},
        specialist={"verified_completion": 0.9, "total_cost": 12.0,
                    "end_to_end_latency": 100.0})
    assert out["case"] == "tradeoff", \
        "a regression alongside the improvement is an explicit tradeoff"
    assert out["overall"]["total_cost"]["verdict"] == "regressed"
    assert out["overall"]["end_to_end_latency"]["verdict"] == "improved"


def test_more_agents_alone_is_not_a_success_metric():
    from backend.services.specialization_eval import evaluate_specialization
    out = evaluate_specialization(
        baseline={"verified_completion": 0.8, "total_cost": 10.0},
        specialist={"verified_completion": 0.8, "total_cost": 15.0})
    assert out["case"] in ("tradeoff", "no_case"), \
        "equal outcomes at higher cost justify nothing"
    # Missing measurements stay MISSING — never zero-filled.
    out2 = evaluate_specialization(baseline={"verified_completion": 0.8},
                                   specialist={})
    assert out2["case"] == "unmeasured"
    assert "total_cost" in out2["unmeasured"]


# ── §15.24: learned selection through the controlled path ──────────────

@pytest.mark.asyncio
async def test_learned_selection_cannot_leave_the_eligible_set(env):
    from backend.services.contextual_policy import ContextualRoutingPolicy
    from backend.services.delegation_policy import (
        eligible_specialist_actions, select_specialist)
    policy = ContextualRoutingPolicy(env["stats"])
    out = await select_specialist(
        policy, task_category="research", capability="verification",
        candidates=["outcome_verifier", "recovery_analyst"],
        rng=__import__("random").Random(2))
    assert out["selected_role"] in {"outcome_verifier", "recovery_analyst"}
    assert out["selection_probability"] in (0.9, 0.1), \
        "the logged probability describes the actual mechanism"


@pytest.mark.asyncio
async def test_learned_selection_cannot_bypass_authority_or_limits(tmp_path):
    """Even if selection picks a role, the delegation layer enforces the
    role whitelist and the tree bounds — selection is NOT authority."""
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.services.agent_contracts import delegate
    from backend.services.contextual_policy import ContextualRoutingPolicy
    from backend.services.delegation_policy import select_specialist
    db = DatabaseManager(str(tmp_path / "sel.db"))
    await db.initialize()
    repo = AgentTaskRepository(db)
    set_agent_task_repo(repo)
    stats = SQLiteAdaptivePolicyRepository(db)
    try:
        policy = ContextualRoutingPolicy(stats)
        out = await select_specialist(
            policy, task_category="workflow", capability="x",
            candidates=["outcome_verifier"], rng=__import__("random").Random(1))
        assert out["selected_role"] == "outcome_verifier"
        # The delegation layer STILL refuses out-of-boundary authority.
        from backend.models.agent_task import AgentRole, AgentTaskError
        with pytest.raises(AgentTaskError, match="authority boundary"):
            await delegate(
                parent_kind="run", parent_id="run-sel",
                role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                goal="g", input_data={"x": 1},
                output_schema={"required": ["ok"]},
                acceptance_criteria=[{"kind": "non_empty"}],
                allowed_tools=["mcp.dispatch"],   # NOT on the whitelist
                repo=repo)
    finally:
        set_agent_task_repo(None)
        await db.close()


@pytest.mark.asyncio
async def test_selection_requires_the_contextual_policy(env):
    from backend.services.delegation_policy import select_specialist
    with pytest.raises(ValueError, match="contextual"):
        await select_specialist(object(), task_category="workflow")


def SQLiteAdaptivePolicyRepository(db):   # local helper for the test above
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository as _R)
    return _R(db)
