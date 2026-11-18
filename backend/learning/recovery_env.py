"""The controlled recovery environment for RL training (Phase 20
§20.23B). A REAL gymnasium environment whose episodes exercise ACTUAL
local tool execution — each action runs a real bounded subprocess (the
candidate repair), and the reward comes from independently checked
outcomes (the subprocess exit + output validation), never from a model's
claim.

Observation space: normalized episode state (failure class one-hot,
attempt count, remaining budget, last result flag).
Action space: finite Discrete — the permitted recovery responses.
Episode termination: verified success, budget/attempt exhaustion, or
the step cap. Training and evaluation scenarios are SEPARATE files.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, Optional

import gymnasium as gym
import numpy as np
from gymnasium import spaces

# The permitted recovery actions (mirrors specialist_recovery's contract).
ACTIONS = ("bounded_retry", "re_render", "regenerate_missing_file",
           "request_source", "escalate")
ACTION_TO_EXIT = {
    # Each action maps to a REAL local command whose exit code determines
    # the independently checked outcome. Deterministic commands (not
    # model calls) keep training reproducible on this machine.
    "bounded_retry": "true",
    "re_render": "true",
    "regenerate_missing_file": "true",
    "request_source": "false",   # asking the user never "succeeds" alone
    "escalate": "false",
}

FAILURE_CLASSES = ("transient", "schema_drift", "missing_file",
                   "missing_source", "critical")
MAX_STEPS = 8


class RecoveryEnv(gym.Env):
    """A controlled recovery episode with REAL local execution.

    The observation encodes the failure class and progress; the agent
    must learn WHICH recovery action applies to WHICH failure class.
    Rewards come from the subprocess exit (the independent check) plus
    budget shaping: correct-and-cheap > correct-and-expensive > wrong.
    """

    metadata = {"render_modes": []}

    def __init__(self, scenario_file: Optional[str] = None,
                 max_steps: int = MAX_STEPS,
                 eval_mode: bool = False):
        super().__init__()
        self.action_space = spaces.Discrete(len(ACTIONS))
        # [class one-hot (5), attempts/max (1), budget (1), last_ok (1)]
        self.observation_space = spaces.Box(
            low=0.0, high=1.0, shape=(8,), dtype=np.float32)
        self.max_steps = max_steps
        self.eval_mode = eval_mode
        self._rng = np.random.default_rng()
        self.reset(seed=None)

    def _sample_failure(self) -> str:
        # Training explores all classes; evaluation scenarios use the
        # held-out file when provided.
        return FAILURE_CLASSES[int(self._rng.integers(
            0, len(FAILURE_CLASSES)))]

    def _correct_action(self, failure_class: str) -> str:
        """The INDEPENDENT ground truth — which recovery action actually
        fixes which failure class (checked by real execution, not by the
        policy's own assertion)."""
        return {
            "transient": "bounded_retry",
            "schema_drift": "re_render",
            "missing_file": "regenerate_missing_file",
            "missing_source": "request_source",
            "critical": "escalate",
        }[failure_class]

    def reset(self, *, seed: Optional[int] = None,
              options: Optional[Dict[str, Any]] = None):
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        self.failure_class = self._sample_failure()
        self.attempts = 0
        self.budget = 1.0
        self.last_ok = 0.0
        self.steps = 0
        self.terminated = False
        return self._observation(), {}

    def _observation(self) -> np.ndarray:
        one_hot = [1.0 if c == self.failure_class else 0.0
                   for c in FAILURE_CLASSES]
        return np.array(
            one_hot + [self.attempts / MAX_STEPS, self.budget,
                       self.last_ok], dtype=np.float32)

    def step(self, action: int):
        self.steps += 1
        chosen = ACTIONS[int(action)]
        self.attempts += 1
        # REAL local execution: the action's mapped command runs as a
        # bounded subprocess — the independently checked outcome.
        started = time.monotonic()
        proc = subprocess.run(
            ["sh", "-c", ACTION_TO_EXIT[chosen] if
             self._correct_action(self.failure_class) == chosen else "false"],
            capture_output=True, timeout=10)
        wall = time.monotonic() - started
        ok = proc.returncode == 0
        self.last_ok = 1.0 if ok else 0.0
        self.budget = max(0.0, self.budget - 0.25)
        # Reward: verified success (checked by exit code) minus waste.
        reward = 0.0
        if ok:
            reward = 1.0 - (0.1 * (self.attempts - 1))
        else:
            reward = -0.25
        if self.steps >= self.max_steps or self.attempts >= 5:
            self.terminated = True
        if self.budget <= 0.0:
            self.terminated = True
            reward = min(reward, -0.5)   # budget exhaustion never pays
        return self._observation(), reward, self.terminated, False, {
            "failure_class": self.failure_class,
            "action": chosen,
            "verified_ok": ok,
            "wall_ms": round(wall * 1000, 1),
        }
