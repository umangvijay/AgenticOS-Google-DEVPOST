"""§21.18: train + evaluate + GOVERNED promotion for the real recovery
environment. The promotion decision is governance-based: the candidate
policy is evaluated on HELD-OUT task-class seeds against the recorded
baseline, and promotion happens through the ImprovementService gate
(human approval, typed evidence) — never a silent checkpoint copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("train_real_recovery")

PROMOTION_THRESHOLD = 0.80


def train(total_timesteps: int = 60000, seed: int = 7, out_dir: str = None,
          num_envs: int = 1):
    from stable_baselines3 import PPO
    from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv
    from backend.learning.real_recovery_env import RealRecoveryEnv

    out_dir = out_dir or os.path.join("data", "rl", "real_recovery")
    os.makedirs(out_dir, exist_ok=True)

    def make_env(rank):
        def _init():
            env = RealRecoveryEnv()
            env.reset(seed=seed + rank)
            return env
        return _init

    if num_envs < 1 or num_envs > 32 or total_timesteps < 1:
        raise ValueError("positive training steps and 1–32 environments required")
    # In-process vectorization is a supported compute profile, using the same
    # real state-changing environment. Parallel execution is explicit.
    envs = (DummyVecEnv if num_envs == 1 else SubprocVecEnv)(
        [make_env(i) for i in range(num_envs)])
    try:
        model = PPO("MlpPolicy", envs, seed=seed, verbose=0,
                    n_steps=256, batch_size=128)
        model.learn(total_timesteps=total_timesteps)
        checkpoint = os.path.join(out_dir, f"real_recovery_ppo_{seed}.zip")
        model.save(checkpoint)
    finally:
        envs.close()
    return checkpoint


def evaluate(checkpoint: str | None, seeds=(4101, 5201, 6301),
             episodes_per_seed: int = 12, *, max_steps: int = 100) -> dict:
    """Held-out evaluation: per-task-class accuracy over seeds NOT used
    in training. Returns the mean accuracy + per-class detail."""
    import random
    from backend.learning.real_recovery_env import (
        RealRecoveryEnv, ACTIONS)
    seeds = tuple(seeds)
    if not seeds or episodes_per_seed < 1 or max_steps < 1:
        raise ValueError("evaluation requires seeds, positive episode and step counts")
    model = None
    if checkpoint is not None:
        from stable_baselines3 import PPO
        model = PPO.load(checkpoint)
    per_class = {}
    episodes = []
    total, solved = 0, 0
    for seed in seeds:
        env = RealRecoveryEnv()
        try:
            for episode in range(episodes_per_seed):
                # Stable, independent task instances shared by candidate and
                # baseline. No wall-clock or process-global RNG dependency.
                episode_seed = int.from_bytes(hashlib.sha256(
                    f"recovery-eval-v1:{seed}:{episode}".encode()).digest()[:4], "big")
                rng = random.Random(episode_seed)
                obs, _ = env.reset(seed=episode_seed)
                terminated = truncated = False
                info = {}
                for step in range(1, max_steps + 1):
                    action = (model.predict(obs, deterministic=True)[0] if model
                              is not None else rng.randrange(len(ACTIONS)))
                    obs, _, terminated, truncated, info = env.step(int(action))
                    if terminated or truncated:
                        break
                success = bool(terminated and info.get("verified_solved"))
                total += 1
                solved += int(success)
                task_class = info.get("task_class", "unknown")
                stats = per_class.setdefault(task_class, [0, 0])
                stats[0] += int(success)
                stats[1] += 1
                episodes.append({"seed": episode_seed, "task_class": task_class,
                                 "solved": success, "steps": step,
                                 "terminated": bool(terminated),
                                 "truncated": bool(truncated or not terminated)})
        finally:
            env.close()
    accuracy = solved / max(1, total)
    detail = {cls: {"solved": v[0], "episodes": v[1]}
              for cls, v in sorted(per_class.items())}
    return {"accuracy": round(accuracy, 3), "episodes": total,
            "per_class": detail, "seeds": list(seeds),
            "episodes_per_seed": episodes_per_seed, "solved": solved,
            "episode_results": episodes,
            "policy": "checkpoint" if checkpoint else "seeded_random_baseline"}


def _deterministic_baseline(seeds, episodes_per_seed):
    """§27.D9/§29.25: the production deterministic recovery policy as a
    MEANINGFUL baseline — an operator policy that derives its FIRST
    action from the SAME 12-float observation the learned policy sees
    (the hidden task-class label is never read), evaluated in the same
    environment over the same seeds. Random-only baselines and
    label-reading policies cannot show diagnostic intelligence."""
    import torch  # noqa: F401 — environment import requires it
    from backend.learning.real_recovery_env import (
        RealRecoveryEnv, ACTIONS, TASK_CLASSES)

    def first_action_from_observation(obs):
        """The operator's triage from OBSERVED evidence only: poisoned
        artifacts escalate; a down service is probed; a present-but-
        invalid artifact is re-rendered; a missing artifact is
        regenerated (a failed regenerate then requests the source)."""
        poisoned = obs[9] > 0.5
        artifact_ok = obs[4] > 0.5
        artifact_valid = obs[5] > 0.5
        service_down = obs[6] > 0.5 and obs[7] > 0.0
        if poisoned:
            return "escalate"
        if service_down:
            return "bounded_retry"
        if artifact_ok and not artifact_valid:
            return "re_render"
        if not artifact_ok:
            return "regenerate_missing_file"
        return "bounded_retry"

    total = solved = 0
    per_class = {}
    episodes = []
    for seed in seeds:
        for _ in range(episodes_per_seed):
            episode_seed = seed * 1009 + len(episodes)
            env = RealRecoveryEnv(work_root=None)
            obs, _ = env.reset(seed=episode_seed)
            task_class = env.task_class  # SCORING label only — never read
            # by the policy; retained for the per-case report.
            first = first_action_from_observation(obs)
            action = ACTIONS.index(first)
            regenerate_failed = False
            source_requested = False
            chosen_actions = []
            step = 0
            try:
                for step in range(1, env.max_steps + 1):
                    chosen_actions.append(ACTIONS[action])
                    obs, _, terminated, truncated, info = env.step(action)
                    if terminated or truncated:
                        break
                    chosen = ACTIONS[action]
                    if (chosen == "regenerate_missing_file"
                            and not env.last_ok):
                        regenerate_failed = True
                    if regenerate_failed and not source_requested:
                        # The two-phase task: the source must be
                        # requested before a regenerate can succeed.
                        action = ACTIONS.index("request_source")
                        source_requested = True
                    else:
                        action = ACTIONS.index(first)
                success = bool(terminated and info.get("verified_solved"))
            finally:
                env.close()
            total += 1
            solved += int(success)
            stats = per_class.setdefault(task_class, [0, 0])
            stats[0] += int(success)
            stats[1] += 1
            episodes.append({"seed": episode_seed, "task_class": task_class,
                             "solved": success, "steps": step,
                             "actions": chosen_actions})
    accuracy = solved / max(1, total)
    return {"accuracy": round(accuracy, 3), "episodes": total,
            "solved": solved, "policy": "deterministic_recovery_policy",
            "observation_derived": True,
            "per_class": {cls: {"solved": v[0], "episodes": v[1]}
                          for cls, v in sorted(per_class.items())},
            "episode_results": episodes}


def promotion_floor(baseline: dict, deterministic: dict) -> float:
    """§28.6/§29.25: the promotion comparison uses the MEANINGFUL floor —
    the deterministic recovery policy — not the seeded-random baseline.
    A candidate is eligible for review only when it beats BOTH the
    deterministic floor and the random baseline's best."""
    return max(baseline["accuracy"], deterministic["accuracy"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timesteps", type=int, default=60000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--eval-only", action="store_true")
    parser.add_argument("--num-envs", type=int, default=1)
    parser.add_argument("--out-dir", default=os.path.join("data", "rl", "real_recovery"))
    args = parser.parse_args()
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    if args.eval_only:
        checkpoint = os.path.join(out_dir, f"real_recovery_ppo_{args.seed}.zip")
        report = evaluate(checkpoint)
    else:
        checkpoint = train(args.timesteps, args.seed, out_dir, args.num_envs)
        report = evaluate(checkpoint)
    baseline = evaluate(None)
    deterministic = _deterministic_baseline(seeds=(4101, 5201, 6301),
                                            episodes_per_seed=4)
    comparison_floor = promotion_floor(baseline, deterministic)
    record = {
        "checkpoint": checkpoint,
        "checkpoint_sha256": hashlib.sha256(Path(checkpoint).read_bytes()).hexdigest(),
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "timesteps": args.timesteps,
        "seed": args.seed,
        "evaluation": report,
        "baseline_accuracy": baseline["accuracy"],
        "baseline_evaluation": baseline,
        "deterministic_baseline": deterministic,
        "comparison_floor": comparison_floor,
        "promotion_threshold": PROMOTION_THRESHOLD,
        "governed_promotion": {
            "decision": ("eligible_for_review" if report["accuracy"]
                         >= PROMOTION_THRESHOLD
                         and report["accuracy"] > comparison_floor
                         else "keep_baseline"),
            "note": "promotion still requires ImprovementService "
                    "human approval (§13.4) — this record is EVIDENCE, "
                    "not authorization",
        },
    }
    out = os.path.join(out_dir, "training-record.json")
    with open(out, "w") as fh:
        json.dump(record, fh, indent=2)
    print(json.dumps(record, indent=2))
    return record


if __name__ == "__main__":
    main()
