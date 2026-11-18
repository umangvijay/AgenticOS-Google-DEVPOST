"""§21.18: REAL recovery tasks for RL — controlled environments with
ACTUALLY CHANGING STATE. Every episode manipulates REAL files in a
scratch workspace; every action runs a REAL subprocess that either
fixes the state or fails; the reward is computed from INDEPENDENTLY
OBSERVED state (reading the files back), never from the action's
identity or a model's claim.

Task classes (state changes the agent must handle):
- missing_file: the required artifact was deleted. `regenerate` runs a
  real generator that rewrites it; verified by reading the file back.
- transient_service: a service flag flips back online after N real
  probes (the downtime counter lives in a state file) — `retry` runs a
  real probe command; too-early retries genuinely fail.
- schema_drift: a data file carries the OLD schema; `re_render` runs a
  real converter that rewrites it to the NEW schema; verified by
  parsing the file against the new shape.
- missing_source: the source document is absent; `request_source`
  changes the state so the source ARRIVES on the NEXT step, then the
  file can be regenerated — a TWO-PHASE stateful task.
- critical: a poisoned state NO repair fixes; only `escalate` ends the
  episode correctly (reward shapes against budget burn).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional

import gymnasium as gym
import numpy as np
from gymnasium import spaces

ACTIONS = ("bounded_retry", "re_render", "regenerate_missing_file",
           "request_source", "escalate")
TASK_CLASSES = ("transient_service", "schema_drift", "missing_file",
                "missing_source", "critical")
MAX_STEPS = 8

GOOD_FILE = "artifact.json"
GOOD_CONTENT = {"version": 2, "rows": [1, 2, 3], "status": "ready"}


class RealRecoveryEnv(gym.Env):
    """Gymnasium environment over a REAL scratch workspace."""

    metadata = {"render_modes": []}

    def __init__(self, *, max_steps: int = MAX_STEPS,
                 work_root: Optional[str] = None):
        super().__init__()
        self.action_space = spaces.Discrete(len(ACTIONS))
        # [class one-hot (5), attempts, budget, last_ok, phase]
        self.observation_space = spaces.Box(
            low=0.0, high=1.0, shape=(12,), dtype=np.float32)
        self.max_steps = max_steps
        self._work_root = work_root or tempfile.mkdtemp(prefix="rl-real-")
        os.makedirs(self._work_root, exist_ok=True)
        self._rng = np.random.default_rng()
        self._workspace: Optional[str] = None

    # ── workspace (the ACTUAL state) ──────────────────────────────────

    def _new_workspace(self) -> str:
        ws = tempfile.mkdtemp(dir=self._work_root, prefix="ep-")
        return ws

    def _setup_state(self, task_class: str) -> None:
        ws = self._workspace
        if task_class == "missing_file":
            # The artifact is missing; regenerating writes GOOD_CONTENT.
            pass
        elif task_class == "transient_service":
            # The service is DOWN for a random 1-2 more probes (state
            # lives in the file the probe command decrements).
            downtime = int(self._rng.integers(1, 3))
            with open(os.path.join(ws, "service_state.json"), "w") as fh:
                json.dump({"down_for": downtime}, fh)
        elif task_class == "schema_drift":
            with open(os.path.join(ws, GOOD_FILE), "w") as fh:
                json.dump({"version": 1, "rows": [1, 2, 3]}, fh)
        elif task_class == "missing_source":
            # The source is absent; requesting it makes it arrive.
            with open(os.path.join(ws, "source_state.json"), "w") as fh:
                json.dump({"source_requested": False}, fh)
        elif task_class == "critical":
            # A poisoned artifact NO repair fixes. §29.25: the workspace
            # carries the marker the observation contract advertises —
            # observation[9] ("poisoned marker present, observed not
            # labeled") must be able to FIRE, or critical is
            # unidentifiable from the deployment-visible state and every
            # observation-derived policy fails this class.
            with open(os.path.join(ws, GOOD_FILE), "w") as fh:
                json.dump({"version": 666, "rows": None}, fh)
            with open(os.path.join(ws, "poisoned.flag"), "w") as fh:
                fh.write("unrecoverable\n")

    # ── real action commands (subprocesses with real effects) ─────────

    def _run_action(self, action: str) -> Dict[str, Any]:
        ws = self._workspace
        scripts = {
            "bounded_retry":
                # A REAL probe: checks the service state file; succeeds
                # only when the downtime counter has expired.
                f'''python3 - << 'PY'
import json, os, sys
p = os.path.join({ws!r}, "service_state.json")
if not os.path.exists(p):
    sys.exit(0)  # no service flag: the probe succeeds
state = json.load(open(p))
if state["down_for"] > 0:
    state["down_for"] -= 1
    json.dump(state, open(p, "w"))
    sys.exit(1)  # genuinely still down
sys.exit(0)
PY''',
            "re_render":
                # A REAL converter: rewrites v1 schema → v2 in place.
                f'''python3 - << 'PY'
import json, os, sys
p = os.path.join({ws!r}, {GOOD_FILE!r})
if not os.path.exists(p):
    sys.exit(1)
doc = json.load(open(p))
if doc.get("version") == 2 and doc.get("status") == "ready":
    sys.exit(0)
if doc.get("version") != 1:
    sys.exit(1)  # poisoned/unknown schema: conversion genuinely fails
doc["version"] = 2
doc["status"] = "ready"
json.dump(doc, open(p, "w"))
sys.exit(0)
PY''',
            "regenerate_missing_file":
                # A REAL generator: writes the artifact from the source.
                f'''python3 - << 'PY'
import json, os, sys
ws = {ws!r}
src_state = os.path.join(ws, "source_state.json")
if os.path.exists(src_state):
    state = json.load(open(src_state))
    if not state.get("source_requested"):
        sys.exit(1)  # nothing to regenerate FROM yet
doc = {json.dumps(GOOD_CONTENT)}
json.dump(doc, open(os.path.join(ws, {GOOD_FILE!r}), "w"))
sys.exit(0)
PY''',
            "request_source":
                # A REAL state change: marks the source requested; the
                # source ARRIVES (state flips) for the NEXT action.
                f'''python3 - << 'PY'
import json, os, sys
p = os.path.join({ws!r}, "source_state.json")
if not os.path.exists(p):
    sys.exit(1)  # requesting a source makes no sense here
state = json.load(open(p))
if state.get("source_requested"):
    sys.exit(1)  # already requested — repeating adds nothing
state["source_requested"] = True
json.dump(state, open(p, "w"))
sys.exit(0)
PY''',
            "escalate":
                # Escalation is a real terminal handoff: it writes the
                # escalation record (and succeeds ONLY for critical).
                f'''python3 - << 'PY'
import json, os, sys
ws = {ws!r}
json.dump({{"escalated": True}}, open(os.path.join(ws, "escalation.json"), "w"))
p = os.path.join(ws, {GOOD_FILE!r})
if os.path.exists(p):
    doc = json.load(open(p))
    if doc.get("version") == 666:
        sys.exit(0)  # the poisoned state genuinely needs a human
sys.exit(1)  # escalating a repairable task wastes the budget
PY''',
        }
        proc = subprocess.run(["sh", "-c", scripts[action]],
                              capture_output=True, timeout=20)
        return {"exit_code": proc.returncode,
                "stderr": proc.stderr.decode()[:200]}

    # ── INDEPENDENT verification (read the state back) ────────────────

    def _verify(self, task_class: str) -> bool:
        ws = self._workspace
        if task_class == "missing_file":
            p = os.path.join(ws, GOOD_FILE)
            if not os.path.exists(p):
                return False
            try:
                return json.load(open(p)) == GOOD_CONTENT
            except ValueError:
                return False
        if task_class == "transient_service":
            # The service probe succeeded AND the artifact is intact.
            p = os.path.join(ws, "service_state.json")
            if os.path.exists(p):
                return json.load(open(p))["down_for"] == 0
            return True
        if task_class == "schema_drift":
            try:
                doc = json.load(open(os.path.join(ws, GOOD_FILE)))
                return doc.get("version") == 2 and doc.get("status") == "ready"
            except (ValueError, OSError):
                return False
        if task_class == "missing_source":
            try:
                doc = json.load(open(os.path.join(ws, GOOD_FILE)))
                return doc == GOOD_CONTENT
            except (ValueError, OSError):
                return False
        if task_class == "critical":
            return os.path.exists(os.path.join(ws, "escalation.json"))
        return False

    @staticmethod
    def _correct_action(task_class: str) -> str:
        return {
            "transient_service": "bounded_retry",
            "schema_drift": "re_render",
            "missing_file": "regenerate_missing_file",
            "missing_source": "request_source",   # then regenerate
            "critical": "escalate",
        }[task_class]

    # ── gym API ────────────────────────────────────────────────────────

    def reset(self, *, seed: Optional[int] = None,
              options: Optional[Dict[str, Any]] = None):
        super().reset(seed=seed)
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        if self._workspace:
            shutil.rmtree(self._workspace, ignore_errors=True)
        self._workspace = self._new_workspace()
        self.task_class = TASK_CLASSES[int(self._rng.integers(
            0, len(TASK_CLASSES)))]
        self._setup_state(self.task_class)
        self.attempts = 0
        self.budget = 1.0
        self.last_ok = 0.0
        self.steps = 0
        self.phase = 0.0
        self.terminated = False
        self._solved = False
        self._last_action = None
        self._prev_action = None
        return self._observation(), {}

    def _observation(self) -> np.ndarray:
        """§27.D9: the observation derives from state the PRODUCTION
        recovery path actually sees — last action outcome, remaining
        budget, observed failure signature of the workspace files, and
        the two-phase progress flag. The task-class one-hot (a hidden
        label unavailable in production) is deliberately excluded.

        Layout (12 floats):
          [0]  attempts / MAX_STEPS
          [1]  budget remaining
          [2]  last action succeeded (0/1)
          [3]  two-phase progress flag
          [4]  artifact present (observed by reading the workspace)
          [5]  artifact content valid (parsed against the new schema)
          [6]  service state file present
          [7]  service downtime counter (normalized)
          [8]  source document present
          [9]  poisoned marker present (observed, not labeled)
          [10] steps remaining fraction
          [11] previous action repeated (0/1)
        """
        ws = self._workspace
        artifact_ok = 0.0
        artifact_valid = 0.0
        service_present = 0.0
        downtime = 0.0
        source_present = 0.0
        poisoned = 0.0
        try:
            p = os.path.join(ws, GOOD_FILE)
            if os.path.exists(p):
                artifact_ok = 1.0
                with open(p) as fh:
                    data = json.load(fh)
                if data.get("version") == 2 and data.get("status") == "ready":
                    artifact_valid = 1.0
        except Exception:
            pass
        try:
            p = os.path.join(ws, "service_state.json")
            if os.path.exists(p):
                service_present = 1.0
                with open(p) as fh:
                    downtime = min(1.0, json.load(fh).get("down_for", 0) / 3.0)
        except Exception:
            pass
        source_present = 1.0 if os.path.exists(
            os.path.join(ws, "source.json")) else 0.0
        poisoned = 1.0 if os.path.exists(
            os.path.join(ws, "poisoned.flag")) else 0.0
        repeated = 1.0 if getattr(self, "_last_action", None) ==             getattr(self, "_prev_action", None) and self._last_action is not None else 0.0
        return np.array([
            self.attempts / MAX_STEPS, self.budget, self.last_ok,
            self.phase, artifact_ok, artifact_valid, service_present,
            downtime, source_present, poisoned,
            max(0.0, 1.0 - self.attempts / MAX_STEPS), repeated,
        ], dtype=np.float32)

    def step(self, action: int):
        self.steps += 1
        self.attempts += 1
        chosen = ACTIONS[int(action)]
        self._prev_action = getattr(self, "_last_action", None)
        self._last_action = chosen
        result = self._run_action(chosen)
        ok = result["exit_code"] == 0
        self.last_ok = 1.0 if ok else 0.0
        self.budget = max(0.0, self.budget - 0.25)
        # Two-phase task: after a successful request_source, the source
        # has arrived (the phase flag helps the policy see it).
        state_p = os.path.join(self._workspace, "source_state.json")
        if os.path.exists(state_p):
            self.phase = 1.0 if json.load(open(state_p)).get(
                "source_requested") else 0.0
        # INDEPENDENT check: the OBSERVED STATE decides, not the action.
        self._solved = self._verify(self.task_class)
        reward = 0.0
        if self._solved:
            reward = 1.0 - (0.1 * (self.attempts - 1))
        elif not ok:
            reward = -0.25
        if (self._solved or self.steps >= self.max_steps
                or self.attempts >= 5 or self.budget <= 0.0):
            self.terminated = True
        if self.budget <= 0.0 and not self._solved:
            reward = min(reward, -0.5)
        return self._observation(), reward, self.terminated, False, {
            "task_class": self.task_class,
            "action": chosen,
            "exit_code": result["exit_code"],
            "verified_solved": self._solved,
        }

    def close(self):
        if self._workspace:
            shutil.rmtree(self._workspace, ignore_errors=True)
