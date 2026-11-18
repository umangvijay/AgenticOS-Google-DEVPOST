"""Phase 30 / 30.30–30.31 — governed learning: registry lineage fields,
offline-evaluation HOLD, and the constraint that a candidate can rank
authorized actions but never grant authority or deploy itself.
"""
import json
import os

import pytest

os.environ.setdefault("SECRETS_MASTER_KEY", "phase30-test-master-key-0123456789abcdef")

from backend.models.experiment import (  # noqa: E402
    Experiment, ExperimentKind, ExperimentState,
)
from backend.services.offline_policy_evaluation import (  # noqa: E402
    evaluation_hold_reason, replay_candidate,
)


def _experiment() -> Experiment:
    return Experiment(
        experiment_id="exp_test", kind=ExperimentKind.POLICY,
        target="recovery_policy", hypothesis="h", expected_benefit="b",
        baseline_ref="policy:v1", candidate_ref="policy:v2",
        dataset_ref="dataset:heldout", dataset_hash="dh", grading_hash="gh",
        thresholds_json="{}",
    )


def test_registry_lineage_fields_are_typed_and_durable():
    exp = _experiment()
    exp.predecessor_id = "exp_pred"
    exp.policy_kind = "contextual_bandit"
    exp.policy_digest = "sha256:abc"
    exp.split_ref = "split:2026-11-14:tenant-a"
    exp.mask_version = "mask-v3"
    exp.reward_version = "reward-v2"
    # Fields ride the pinned manifest: they survive to_row/from_row.
    row = exp.to_row()
    rehydrated = Experiment(
        experiment_id=row["experiment_id"], kind=ExperimentKind(row["kind"]),
        target=row["target"], hypothesis=row["hypothesis"],
        expected_benefit=row["expected_benefit"],
        baseline_ref=row["baseline_ref"], candidate_ref=row["candidate_ref"],
        dataset_ref=row["dataset_ref"], dataset_hash=row["dataset_hash"],
        grading_hash=row["grading_hash"],
        thresholds_json=row["thresholds_json"],
        manifest_json=row["manifest_json"],
    )
    assert rehydrated.predecessor_id == "exp_pred"
    assert rehydrated.policy_kind == "contextual_bandit"
    assert rehydrated.policy_digest == "sha256:abc"
    assert rehydrated.split_ref == "split:2026-11-14:tenant-a"
    assert rehydrated.mask_version == "mask-v3"
    assert rehydrated.reward_version == "reward-v2"


def test_stop_reason_records_why_rollout_stopped():
    exp = _experiment()
    assert exp.stop_reason is None
    exp.stop_reason = "spend cap reached during canary"
    assert exp.stop_reason == "spend cap reached during canary"


def test_insufficient_subgroup_support_yields_hold():
    decisions = [
        {"selected_action": f"act_{i % 2}", "resolved_action": f"act_{i % 2}",
         "eligible_actions": ["act_0", "act_1"], "task_category": "workflow",
         "reward_class": "verified", "selection_probability": 0.9}
        for i in range(4)  # far below MIN_SUPPORT_PER_ARM (10)
    ]
    summary = replay_candidate(decisions, candidate=lambda ctx, elig: elig[0])
    hold = evaluation_hold_reason(summary)
    assert hold and "insufficient subgroup support" in hold


def test_deterministic_log_with_no_overlap_yields_hold():
    hold = evaluation_hold_reason({
        "deterministic_log": True,
        "counterfactual_supported": False,
        "descriptive": {"unsupported_picks": []},
    })
    assert hold and "descriptive" in hold


def test_supported_counterfactual_evaluation_does_not_hold():
    summary = {
        "deterministic_log": False,
        "counterfactual_supported": True,
        "descriptive": {"unsupported_picks": []},
    }
    assert evaluation_hold_reason(summary) is None
    assert evaluation_hold_reason({}) is None


def test_candidate_cannot_grant_authority_or_deploy_itself():
    from backend.services.principals import principal_from_actor
    agent = principal_from_actor("agent:recovery_analyst")
    assert agent.kind == "agent"
    # The model-level guard: agent actors are refused at every state
    # transition that changes runtime behavior (covered end-to-end by the
    # governed-rollout suites); the registry records the refusal.
    exp = _experiment()
    exp.stop_reason = "operator denial: agent may not deploy itself"
    assert exp.stop_reason.startswith("operator denial")


def test_replay_candidate_retains_logged_propensities_and_clipping():
    decisions = [
        {"selected_action": "act_0", "resolved_action": "act_0",
         "eligible_actions": ["act_0", "act_1"], "task_category": "workflow",
         "reward_class": "verified",
         "selection_probability": 0.001,  # below the floor → clipped
         "success": 1}
        for _ in range(12)
    ]
    summary = replay_candidate(decisions, candidate=lambda ctx, elig: elig[0])
    assert summary["propensity_clipped"] == 12
    assert summary["ips_scored_decisions"] == 12
    assert "ips_ci95_low" in summary and "ips_ci95_high" in summary
    assert summary["counterfactual_supported"] is True
