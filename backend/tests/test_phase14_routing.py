"""Phase 14 acceptance tests: contextual routing with verified rewards.

- §14.1: the reward signal is the VERIFIED outcome joined from durable
  evaluations — an unverified "completed" flag never becomes positive
  reward; partial/uncertain/rejected are tracked separately; missing
  feedback stays explicit.
- §14.3: only permitted/configured actions enter the eligible set.
- §14.4: the decision log carries context, eligible actions, choice,
  probability, policy version — and feedback joins EXACTLY once
  (redelivery cannot rewrite it).
- §14.5: offline comparison reports coverage/uncertainty; unsupported
  picks are flagged, never silently averaged.
- Delayed/missing feedback, availability changes, drift, and fixed-
  baseline comparison all covered.
"""
import pytest
import pytest_asyncio

from backend.models.routing_decision import RewardClass, RoutingDecision, outcome_class_for
from backend.repositories.routing_repository import (
    RoutingDecisionRepository, new_decision_id, set_routing_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.offline_policy_evaluation import (
    MIN_SUPPORT_PER_ARM, compare, fixed_policy, policy_value, replay_candidate)
from backend.services.routing_recorder import (
    attach_evaluation_feedback, record_model_selection,
)


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "routing.db"))
    await db.initialize()
    r = RoutingDecisionRepository(db)
    set_routing_repo(r)
    try:
        yield r
    finally:
        set_routing_repo(None)
        await db.close()


def _decision(run_id="run-1", selected="model-a", resolved=None, **over):
    defaults = dict(
        decision_id=new_decision_id(), run_id=run_id, task_id="t1",
        task_category="research", capability="core.search",
        eligible_actions=["model-a", "model-b", "model-c"],
        selected_action=selected,
        selection_mechanism=("ranked_first_fit_fallback" if (resolved and
                                                             resolved != selected)
                             else "ranked_first_fit"),
        selection_probability=1.0,
        policy_version="ucb1-v1-verified-feedback",
        resolved_action=resolved or selected)
    defaults.update(over)
    d = RoutingDecision(**defaults)
    return d


# ── §14.4: the decision log ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decision_log_captures_context_and_choice(repo):
    await record_model_selection(
        run_id="run-log", task_id="t9",
        eligible_actions=["model-a", "model-b"],
        selected_action="model-a", task_category="research",
        capability="core.search", input_size_bytes=2048)
    rows = await repo.decisions_for_run("run-log")
    assert len(rows) == 1
    d = rows[0]
    assert d["eligible_actions"] == ["model-a", "model-b"]
    assert d["selected_action"] == "model-a"
    # §13.6: the mechanism that ACTUALLY ran is deterministic ranked
    # first-fit — labeled as such, with probability 1.0 (never uniform
    # random).
    assert d["selection_mechanism"] == "ranked_first_fit"
    assert d["selection_probability"] == 1.0
    assert d["reward_class"] == "missing", "feedback starts explicit-missing"
    assert d["task_category"] == "research"


@pytest.mark.asyncio
async def test_feedback_joins_exactly_once(repo):
    """§14.4: redelivered terminal events cannot rewrite joined feedback —
    the second attach returns 0 and the reward class is unchanged."""
    await repo.log_decision(_decision(run_id="run-once"))
    first = await repo.attach_feedback("run-once", "verified", True, "eval-1")
    assert first == 1
    second = await repo.attach_feedback("run-once", "rejected", False, "eval-2")
    assert second == 0, "feedback joins EXACTLY once"
    rows = await repo.decisions_for_run("run-once")
    assert rows[0]["reward_class"] == "verified"
    assert rows[0]["evaluation_id"] == "eval-1"


@pytest.mark.asyncio
async def test_fallback_lineage_is_logged(repo):
    """§12.15: when the provider falls back, the resolved model and the
    fallback source are both recorded."""
    await record_model_selection(
        run_id="run-fb", task_id="t1", eligible_actions=["m-a", "m-b"],
        selected_action="m-a")
    await repo.log_decision(_decision(run_id="run-fb", selected="m-a",
                                      resolved="m-b", fallback_from="m-a"))
    rows = await repo.decisions_for_run("run-fb")
    assert rows[-1]["resolved_action"] == "m-b"
    assert rows[-1]["fallback_from"] == "m-a"
    assert rows[-1]["selection_mechanism"] == "ranked_first_fit_fallback"


# ── §14.1: the verified reward classification ──────────────────────────

def test_outcome_classification_never_upgrades_unverified():
    assert outcome_class_for({"success": True}) == "verified"
    assert outcome_class_for({"success": False, "needs_review_tasks": ["t1"]}) \
        == "uncertain"
    assert outcome_class_for({"success": False,
                              "partially_applied_effects": ["fx1"]}) == "partial"
    assert outcome_class_for({"success": False,
                              "uncertain_effects": ["fx1"]}) == "uncertain"
    assert outcome_class_for({"success": False}) == "rejected"
    assert outcome_class_for(None) == "missing"


@pytest.mark.asyncio
async def test_verified_feedback_updates_policy_only_from_verified_or_rejected(repo):
    """§14.1/§12.35: verified→positive reward; rejected→negative; partial
    and uncertain NEVER touch the learned stats."""
    from backend.services.adaptive_policy import AdaptivePolicy

    class RecordingRepo:
        def __init__(self):
            self.records = []

        async def record_outcome(self, model, success, latency_ms):
            self.records.append((model, success))

        async def get_stats(self):
            return []

    adaptive_repo = RecordingRepo()
    adaptive = AdaptivePolicy(adaptive_repo)

    await repo.log_decision(_decision(run_id="run-v", selected="model-a"))
    await attach_evaluation_feedback("run-v", {"success": True}, "e1",
                                     adaptive=adaptive)
    await repo.log_decision(_decision(run_id="run-r", selected="model-a"))
    await attach_evaluation_feedback("run-r", {"success": False}, "e2",
                                     adaptive=adaptive)
    await repo.log_decision(_decision(run_id="run-u", selected="model-a"))
    await attach_evaluation_feedback(
        "run-u", {"success": False, "uncertain_effects": ["fx"]}, "e3",
        adaptive=adaptive)

    assert ("model-a", True) in adaptive_repo.records
    assert ("model-a", False) in adaptive_repo.records
    # Only two policy updates: uncertain did NOT record anything more.
    assert len(adaptive_repo.records) == 2
    rows = await repo.decisions_for_run("run-u")
    assert rows[0]["reward_class"] == "uncertain"
    assert rows[0]["reward_verified_success"] is None, (
        "uncertain is neither success nor failure in the log")


# ── §14.5: offline comparison with coverage + uncertainty ──────────────

def _logged(n_verified_a=30, n_rejected_a=10, n_verified_b=3,
            category="research"):
    rows = []
    for i in range(n_verified_a):
        rows.append({"selected_action": "model-a", "resolved_action": "model-a",
                     "reward_class": "verified",
                     "task_category": category,
                     "eligible_actions": ["model-a", "model-b"]})
    for i in range(n_rejected_a):
        rows.append({"selected_action": "model-a", "resolved_action": "model-a",
                     "reward_class": "rejected",
                     "task_category": category,
                     "eligible_actions": ["model-a", "model-b"]})
    for i in range(n_verified_b):
        rows.append({"selected_action": "model-b", "resolved_action": "model-b",
                     "reward_class": "verified",
                     "task_category": category,
                     "eligible_actions": ["model-a", "model-b"]})
    return rows


def test_logging_policy_value_counts_only_feedback():
    rows = _logged() + [{"selected_action": "model-a", "reward_class": "missing",
                         "task_category": "research"}]
    out = policy_value(rows)
    assert out["samples"] == 43, "missing feedback is excluded, not zero"
    assert out["by_action"]["model-a"]["value"] == pytest.approx(0.75, abs=0.01)


def test_unsupported_candidate_pick_is_flagged_not_averaged():
    """§14.5: a candidate whose picked action lacks support yields an
    explicit low-confidence estimate — never a silent average."""
    rows = _logged(n_verified_b=MIN_SUPPORT_PER_ARM - 1)   # model-b under-supported
    out = replay_candidate(rows, fixed_policy("model-b"))
    assert out["confidence"] in ("low", "medium")
    # §14.21: the descriptive replay is namespaced — its support flags
    # travel inside it, never as a top-level policy value.
    assert out["descriptive"]["unsupported_picks"] > 0


def test_candidate_cannot_select_outside_eligible_set():
    """§14.3: learning must not discover forbidden actions — a candidate
    picking outside the eligible set is skipped."""
    rows = _logged()
    def forbidden_picker(context, eligible):
        return "model-forbidden"
    out = replay_candidate(rows, forbidden_picker)
    assert out["descriptive"]["supported_estimates"] == 0
    assert all("model-forbidden" not in (a or {}) for a in
               [out["descriptive"]["per_action_picked"]])


def test_fixed_baseline_comparison_reports_both():
    out = compare(_logged(), fixed_policy("model-b"))
    assert "logging_policy" in out and "candidate_policy" in out
    assert out["log_health"]["decisions"] == 43
    a = out["logging_policy"]["by_action"]["model-a"]
    b = out["logging_policy"]["by_action"]["model-b"]
    assert a["sufficient_support"] is True
    assert b["sufficient_support"] is False, (
        "model-b's small sample must be flagged — its raw 1.0 is not "
        "trustworthy despite looking better than model-a's 0.75")


# ── §14.6: availability changes + drift ────────────────────────────────

@pytest.mark.asyncio
async def test_availability_change_narrows_the_eligible_set(repo):
    """When a provider goes away, decisions log the NARROWED eligible set —
    the log reflects what was actually possible at decision time."""
    await record_model_selection(
        run_id="run-avail", task_id="t1",
        eligible_actions=["model-b"],   # model-a unavailable
        selected_action="model-b")
    rows = await repo.decisions_for_run("run-avail")
    assert rows[0]["eligible_actions"] == ["model-b"]
    assert rows[0]["selection_probability"] == 1.0


@pytest.mark.asyncio
async def test_missing_feedback_stays_explicit_in_health(repo):
    await repo.log_decision(_decision(run_id="run-h1"))
    await repo.log_decision(_decision(run_id="run-h2"))
    await repo.attach_feedback("run-h2", "verified", True)
    health = await repo.logging_health()
    assert health["total_decisions"] == 2
    assert health["with_feedback"] == 1
    assert health["missing_feedback"] == 1


# ── §13.6: feedback identity (review finding K) ────────────────────────

@pytest.mark.asyncio
async def test_failed_initial_model_gets_no_credit_from_fallback_success(repo):
    """§13.6 acceptance: candidate A fails transport, fallback B succeeds
    and verifies — the verified reward lands on B's row ONLY; A's decision
    row is never positively rewarded by the fallback."""
    from backend.services.routing_recorder import attach_evaluation_feedback
    from backend.services.adaptive_policy import AdaptivePolicy

    class RecordingRepo:
        def __init__(self):
            self.records = []

        async def record_outcome(self, model, success, latency_ms):
            self.records.append((model, success, latency_ms))

        async def get_stats(self):
            return []

    adaptive_repo = RecordingRepo()
    adaptive = AdaptivePolicy(adaptive_repo)

    await repo.log_decision(_decision(run_id="run-fb2", selected="model-a"))
    await repo.log_decision(_decision(run_id="run-fb2", selected="model-a",
                                      resolved="model-b",
                                      fallback_from="model-a",
                                      latency_ms=812.0))
    out = await attach_evaluation_feedback("run-fb2", {"success": True},
                                           "eval-1", adaptive=adaptive)
    models_rewarded = [r[0] for r in adaptive_repo.records if r[1] is True]
    assert models_rewarded == ["model-b"], (
        "only the RESOLVED model earns the verified reward")
    assert any(r == ("model-b", True, 812.0) for r in adaptive_repo.records), (
        "the measured latency travels with the reward")


@pytest.mark.asyncio
async def test_policy_update_is_idempotent_per_decision(repo):
    """§13.6: a crash between the feedback join and the policy update loses
    nothing (the retry applies) and a retry cannot double-apply."""
    from backend.services.routing_recorder import attach_evaluation_feedback
    from backend.services.adaptive_policy import AdaptivePolicy

    class RecordingRepo:
        def __init__(self):
            self.records = []

        async def record_outcome(self, model, success, latency_ms):
            self.records.append((model, success))

        async def get_stats(self):
            return []

    adaptive_repo = RecordingRepo()
    adaptive = AdaptivePolicy(adaptive_repo)
    await repo.log_decision(_decision(run_id="run-idem", selected="model-a"))
    await attach_evaluation_feedback("run-idem", {"success": True}, "e1",
                                     adaptive=adaptive)
    await attach_evaluation_feedback("run-idem", {"success": True}, "e1",
                                     adaptive=adaptive)
    assert len(adaptive_repo.records) == 1, "applied exactly once"
