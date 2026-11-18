"""Phase 28 §28.6 — the governed-promotion comparator and the
recovery-drill dispositions that lacked named coverage.

The retained suites cover episode claims, competing workers, deadline
escalation, misleading-success refusal, and the worker-process governed
rollout (baseline → candidate → rollback). This file pins the audit's
remaining gap: the training record's promotion decision must compare
against the DETERMINISTIC recovery-policy floor — a candidate that only
beats the seeded-random baseline is not diagnostic intelligence.
"""
import json
import os

import pytest

from backend.learning import train_real_recovery_policy as trainer


@pytest.fixture
def training_env(tmp_path, monkeypatch):
    """A fake checkpoint + patched evaluators so main() runs without any
    real PPO training, deterministically."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    checkpoint = out_dir / "real_recovery_ppo_3.zip"
    checkpoint.write_bytes(b"fixture-checkpoint")
    state = {"candidate": 0.9, "random": 0.5, "deterministic": 0.95}

    def fake_evaluate(checkpoint, seeds=None, episodes_per_seed=12,
                      max_steps=100):
        accuracy = (state["candidate"] if checkpoint is not None
                    else state["random"])
        return {"accuracy": accuracy, "episodes": 12, "solved": 12,
                "per_class": {}, "seeds": list(seeds or ()),
                "episodes_per_seed": episodes_per_seed,
                "episode_results": [],
                "policy": "checkpoint" if checkpoint else "seeded_random_baseline"}

    monkeypatch.setattr(trainer, "evaluate", fake_evaluate)
    monkeypatch.setattr(
        trainer, "_deterministic_baseline",
        lambda seeds, episodes_per_seed: {
            "accuracy": state["deterministic"], "episodes": 12, "solved": 12,
            "policy": "deterministic_recovery_policy", "per_class": {},
            "episode_results": []})
    import sys
    monkeypatch.setattr(sys, "argv", [
        "train", "--eval-only", "--seed", "3",
        "--out-dir", str(out_dir)])
    return state, out_dir / "training-record.json"


def _run(training_env):
    state, record_path = training_env
    record = trainer.main()
    with open(record_path) as fh:
        persisted = json.load(fh)
    assert persisted == record, "the persisted training record is the returned one"
    return record


def test_candidate_that_only_beats_the_random_baseline_is_rejected(training_env):
    state, _ = training_env
    # Beats the random baseline (0.5) and the threshold (0.80), but LOSES
    # to the deterministic recovery policy (0.95): keep_baseline.
    state["candidate"] = 0.90
    record = _run(training_env)
    assert record["governed_promotion"]["decision"] == "keep_baseline"
    assert record["comparison_floor"] == 0.95


def test_candidate_that_beats_the_deterministic_floor_is_review_eligible(training_env):
    state, _ = training_env
    state["candidate"] = 0.99
    record = _run(training_env)
    assert record["governed_promotion"]["decision"] == "eligible_for_review"
    assert "ImprovementService" in record["governed_promotion"]["note"]
    # The three records stay DISTINCT: checkpoint evidence, the review
    # decision, and (separately) ImprovementService activation authority.
    assert record["checkpoint_sha256"]
    assert record["deterministic_baseline"]["policy"] == \
        "deterministic_recovery_policy"


def test_poor_candidate_is_rejected_even_below_threshold(training_env):
    state, _ = training_env
    state["candidate"] = 0.40
    record = _run(training_env)
    assert record["governed_promotion"]["decision"] == "keep_baseline"
