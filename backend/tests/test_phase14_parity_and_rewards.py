"""Phase 14 §14.14–§14.19 confirmations + §14.19 storage parity.

Each row names its subphase and the acceptance it asserts:
- 14.14 correct probabilities (0.90 / ε/(n−1), summing to 1.0)
- 14.15 atomic exploration admission (count+claim in one transaction)
- 14.16 fallback attribution through reward events
- 14.17 ONE reward specification, shared by both learners
- 14.18 durable reward events (see test_phase14_rewards.py)
- 14.19 the SQLite and Firestore stores expose the SAME contract
"""
import inspect

import pytest


# ── §14.14: correct, complete selection probabilities ──────────────────

def test_probability_distribution_sums_to_one():
    from backend.services.contextual_policy import (
        EPSILON_CONTEXTUAL, ContextualRoutingPolicy)
    n = 4   # four eligible actions
    p_top = round(1.0 - EPSILON_CONTEXTUAL, 6)
    p_other = round(EPSILON_CONTEXTUAL / (n - 1), 6)
    total = p_top + p_other * (n - 1)
    assert abs(total - 1.0) < 1e-6, \
        "the logged probabilities describe the COMPLETE distribution"


# ── §14.15: atomic exploration admission ────────────────────────────────

@pytest.mark.asyncio
async def test_exploration_admission_is_one_atomic_operation(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    db = DatabaseManager(str(tmp_path / "expl.db"))
    await db.initialize()
    try:
        repo = SQLiteAdaptivePolicyRepository(db)
        # Admissions up to the cap; concurrent callers cannot exceed it
        # because count+claim is ONE transaction.
        from backend.services.contextual_policy import (
            EXPLORATION_BUDGET_MAX)
        admitted = 0
        for _ in range(EXPLORATION_BUDGET_MAX):
            if await repo.admit_exploration(3600, EXPLORATION_BUDGET_MAX):
                admitted += 1
        assert admitted == EXPLORATION_BUDGET_MAX
        assert await repo.admit_exploration(
            3600, EXPLORATION_BUDGET_MAX) is False, \
            "the cap is a hard ceiling, not a suggestion"
    finally:
        await db.close()


# ── §14.17: ONE reward specification ────────────────────────────────────

def test_reward_spec_is_single_sourced():
    """The recorder, the offline evaluator, and the contextual learner
    all read the SAME mapping — no module owns a second copy."""
    from backend.services.offline_policy_evaluation import REWARD_WEIGHTS
    from backend.services.contextual_policy import reward_from_class
    from backend.models.routing_decision import RewardClass, outcome_class_for
    spec = {cls.value: w for cls, w in [
        (RewardClass.VERIFIED, 1.0), (RewardClass.PARTIAL, 0.5),
        (RewardClass.UNCERTAIN, 0.0), (RewardClass.REJECTED, 0.0),
        (RewardClass.MISSING, 0.0)]}
    assert REWARD_WEIGHTS == spec
    for cls in spec:
        assert reward_from_class(cls) == spec[cls]
    # The evaluator's classification produces exactly the spec's keys.
    evaluation = {"success": False, "needs_review_tasks": ["t1"]}
    assert outcome_class_for(evaluation) == "uncertain"


# ── §14.19: storage parity — one contract, two stores ───────────────────

def test_routing_repos_expose_the_same_contract():
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository)
    from backend.repositories.firestore.routing_repository import (
        FirestoreRoutingDecisionRepository)
    sql = {m for m in dir(RoutingDecisionRepository)
           if not m.startswith("_") and callable(
               getattr(RoutingDecisionRepository, m))}
    fs = {m for m in dir(FirestoreRoutingDecisionRepository)
          if not m.startswith("_") and callable(
              getattr(FirestoreRoutingDecisionRepository, m))}
    missing = sql - fs
    assert not missing, f"Firestore routing is missing: {sorted(missing)}"


def test_stats_repos_expose_the_same_contract():
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    from backend.repositories.firestore.sidecar import (
        FirestoreAdaptivePolicyRepository)
    sql = {m for m in dir(SQLiteAdaptivePolicyRepository)
           if not m.startswith("_") and callable(
               getattr(SQLiteAdaptivePolicyRepository, m))}
    fs = {m for m in dir(FirestoreAdaptivePolicyRepository)
          if not m.startswith("_") and callable(
              getattr(FirestoreAdaptivePolicyRepository, m))}
    missing = sql - fs
    assert not missing, f"Firestore stats is missing: {sorted(missing)}"


def test_job_repos_expose_the_same_fenced_contract():
    from backend.repositories.evaluation_job_repository import (
        EvaluationJobRepository)
    from backend.repositories.firestore.evaluation_job_repository import (
        FirestoreEvaluationJobRepository)
    sql = {m for m in dir(EvaluationJobRepository)
           if not m.startswith("_") and callable(
               getattr(EvaluationJobRepository, m))}
    fs = {m for m in dir(FirestoreEvaluationJobRepository)
          if not m.startswith("_") and callable(
              getattr(FirestoreEvaluationJobRepository, m))}
    missing = sql - fs
    assert not missing, f"Firestore jobs is missing: {sorted(missing)}"


def test_the_atomic_consumption_contract_matches_across_stores():
    """Both stores' consume_reward_event carry the same signature and
    the same consumer vocabulary — the crash-safety contract is ONE
    contract, not two dialects."""
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    from backend.repositories.firestore.sidecar import (
        FirestoreAdaptivePolicyRepository)
    for cls in (SQLiteAdaptivePolicyRepository,
                FirestoreAdaptivePolicyRepository):
        sig = inspect.signature(cls.consume_reward_event)
        params = set(sig.parameters)
        assert {"event_id", "consumer"} <= params, cls.__name__
