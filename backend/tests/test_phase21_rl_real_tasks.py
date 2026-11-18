"""Phase 21 §21.18: GENUINE RL over REAL recovery tasks. The environment
manipulates actual files (state genuinely changes); rewards come from
independently verified outcomes (reading state back); training changes
network parameters; the checkpoint reloads; held-out evaluation across
seeds is reproducible; promotion is governance-gated, not a checkpoint
copy. Replaces the shell-true/false toy (audit C14)."""
import json
import os

import numpy as np
import pytest

# §28.12: the fixture trains REAL PPO once per pytest process — the
# qualifier runs this file as its own stage AFTER the full unit
# suite and the restore drill, when the host is still settling.
# 900s was measured on a quiet standalone host; 1800s is a
# test-infrastructure bound, not a product bound.
# RL DEFERRED BY OPERATOR (2026-11-14): the governed-learning stage is
# run SEPARATELY when scheduled — genuine PPO training takes ~30 minutes
# standalone (`pytest backend/tests/test_phase21_rl_real_tasks.py -q`).
# The deferral is recorded in the Phase 28 register; the qualification
# verdict keeps rl_training OPEN until then. Nothing about the training
# itself is weakened: unskip and it runs the full genuine curriculum.
pytestmark = [
    pytest.mark.skip(reason="RL deferred by operator — run this file standalone (see register)"),
    pytest.mark.timeout(3000),
]


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    pytest.importorskip("stable_baselines3")
    pytest.importorskip("gymnasium")
    from backend.learning.train_real_recovery_policy import train
    checkpoint = train(total_timesteps=24000, seed=21,
                       out_dir=str(tmp_path_factory.mktemp("rl-training")))
    return checkpoint


def test_environment_state_actually_changes(trained):
    """The hostile fixture: a missing_file episode REALLY creates the
    file; a schema_drift episode REALLY rewrites the schema — verified
    by reading the workspace state, not by trusting the action."""
    from backend.learning.real_recovery_env import (
        RealRecoveryEnv, ACTIONS, GOOD_FILE, GOOD_CONTENT)
    import numpy as _np
    env = RealRecoveryEnv()

    def reset_to(env, wanted: str):
        for attempt in range(200):
            obs, _ = env.reset(seed=int(_np.random.default_rng().integers(
                0, 2**31)))
            if env.task_class == wanted:
                return obs
        raise AssertionError(f"could not sample class {wanted}")

    # missing_file: regenerate → the file REALLY exists with the right bytes.
    obs = reset_to(env, "missing_file")
    ws = env._workspace
    assert not os.path.exists(os.path.join(ws, GOOD_FILE))
    obs, r, done, _, info = env.step(ACTIONS.index("regenerate_missing_file"))
    assert info["verified_solved"] is True
    with open(os.path.join(ws, GOOD_FILE)) as fh:
        assert json.load(fh) == GOOD_CONTENT, "REAL bytes on disk"
    # schema_drift: re_render → the file's schema REALLY changed.
    obs = reset_to(env, "schema_drift")
    doc_before = json.load(open(os.path.join(env._workspace, GOOD_FILE)))
    assert doc_before["version"] == 1
    obs, r, done, _, info = env.step(ACTIONS.index("re_render"))
    doc_after = json.load(open(os.path.join(env._workspace, GOOD_FILE)))
    assert doc_after["version"] == 2 and doc_after["status"] == "ready"
    assert info["verified_solved"] is True
    # transient_service: retries REALLY fail while the service is down
    # (the state file decrements) — a too-early action has real effect.
    obs = reset_to(env, "transient_service")
    state_path = os.path.join(env._workspace, "service_state.json")
    down_before = json.load(open(state_path))["down_for"]
    obs, r, done, _, info = env.step(ACTIONS.index("bounded_retry"))
    if down_before > 0:
        assert info["exit_code"] != 0, \
            "the service was genuinely down — the retry failed"
        down_after = json.load(open(state_path))["down_for"]
        assert down_after == down_before - 1, \
            "the probe CONSUMED one unit of downtime (state changed)"
    # missing_source: two-phase — regenerate FAILS before the request.
    obs = reset_to(env, "missing_source")
    obs, r, done, _, info = env.step(ACTIONS.index("regenerate_missing_file"))
    assert info["exit_code"] != 0, \
        "regenerating without a source genuinely fails (phase 1)"
    obs, r, done, _, info = env.step(ACTIONS.index("request_source"))
    assert info["exit_code"] == 0
    obs, r, done, _, info = env.step(
        ACTIONS.index("regenerate_missing_file"))
    assert info["verified_solved"], "phase 2 completes after the request"
    env.close()


def test_training_changes_parameters_and_checkpoint_loads(trained):
    from stable_baselines3 import PPO
    from backend.learning.real_recovery_env import RealRecoveryEnv
    from stable_baselines3.common.vec_env import DummyVecEnv
    import torch
    env = DummyVecEnv([lambda: RealRecoveryEnv()])
    model = PPO.load(trained, env=env)   # rebind to 1 env
    weights_before = model.policy.state_dict()[
        "action_net.weight"].clone()
    model.learn(total_timesteps=512)
    weights_after = model.policy.state_dict()["action_net.weight"]
    assert not torch.equal(weights_before, weights_after), \
        "training changes network parameters"
    env.close()
    weights_after = model.policy.state_dict()["action_net.weight"]
    assert not torch_equal(weights_before, weights_after), \
        "training changes network parameters"
    env.close()


def torch_equal(a, b):
    import torch
    return torch.equal(a, b)


def test_heldout_eval_reproducible_and_promotion_governed(trained, monkeypatch):
    from backend.learning.train_real_recovery_policy import (
        evaluate, PROMOTION_THRESHOLD)
    r1 = evaluate(trained, seeds=(9101, 9201), episodes_per_seed=6)
    r2 = evaluate(trained, seeds=(9101, 9201), episodes_per_seed=6)
    assert r1["accuracy"] == r2["accuracy"], \
        f"held-out evaluation is reproducible: {r1} vs {r2}"
    assert r1 == r2 and r1["episodes"] == 12
    baseline = evaluate(None, seeds=(9101, 9201), episodes_per_seed=6)
    assert baseline["episodes"] == r1["episodes"]
    assert [e["seed"] for e in baseline["episode_results"]] == [e["seed"] for e in r1["episode_results"]]
    # The recorded promotion decision is governance-shaped: it names the
    # gate rather than claiming automatic authorization.
    from backend.learning.train_real_recovery_policy import main
    import sys
    monkeypatch.setattr(sys, "argv", ["train", "--eval-only", "--seed", "21",
                                     "--out-dir", os.path.dirname(trained)])
    record = main()
    assert record["baseline_accuracy"] == record["baseline_evaluation"]["accuracy"]
    assert record["governed_promotion"]["decision"] in ("eligible_for_review",
                                                        "keep_baseline")
    assert "ImprovementService" in record["governed_promotion"]["note"]
