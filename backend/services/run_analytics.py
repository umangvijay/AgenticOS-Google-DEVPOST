"""
AgentOS — Run Analytics (regression baselines + anomaly detection)

A lightweight, honest statistical layer over real run history:

- REGRESSION: per-agent EWMA duration baselines (mean + variance) learned
  from completed tasks — the expected-duration model.
- ANOMALY DETECTION: z-score against the agent's own baseline flags runs
  that are statistically too slow (ops signal, not a verdict).

No heavy deps: everything is computable from the runs the operations
endpoint already loads. Persisted in SQLite (`agent_stats`) so the model
improves across restarts.
"""

import math
from typing import Any, Dict, List

from backend.repositories.base import BaseAdaptivePolicyRepository


class RunAnalytics:
    """Regression baselines + z-score anomalies from real run history."""

    def __init__(self, stats_repo):
        self.repo = stats_repo

    @staticmethod
    def collect(samples: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        """Welford-style mean/M2 per agent from (agent, duration) samples."""
        stats: Dict[str, Dict[str, float]] = {}
        for s in samples:
            agent = str(s.get("agent") or "unknown")
            duration = s.get("duration")
            if duration is None or duration < 0:
                continue
            st = stats.setdefault(agent, {"n": 0.0, "mean": 0.0, "m2": 0.0})
            st["n"] += 1
            delta = duration - st["mean"]
            st["mean"] += delta / st["n"]
            st["m2"] += delta * (duration - st["mean"])
        return stats

    def analyze(self, runs: List[Dict[str, Any]], z_threshold: float = 2.5) -> Dict[str, List[Dict[str, Any]]]:
        """One pass: baselines (expected durations per agent) + anomalies."""
        samples: List[Dict[str, Any]] = []
        for run in runs:
            started = run.get("created_at")
            for task in run.get("tasks") or []:
                if task.get("status") != "COMPLETED" or not task.get("completed_at") or not started:
                    continue
                try:
                    from datetime import datetime
                    t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(str(task["completed_at"]).replace("Z", "+00:00"))
                    duration = (t1 - t0).total_seconds()
                except (ValueError, TypeError):
                    continue
                if duration >= 0:
                    samples.append({
                        "agent": task.get("agent") or "unknown",
                        "duration": duration,
                        "run_id": run.get("run_id"),
                        "title": task.get("title"),
                    })

        stats = self.collect(samples)
        baselines = [
            {
                "agent": agent,
                "expected_s": round(st["mean"], 2),
                "samples": int(st["n"]),
                "std_s": round(math.sqrt(st["m2"] / st["n"]), 2) if st["n"] else 0.0,
            }
            for agent, st in sorted(stats.items())
        ]

        anomalies: List[Dict[str, Any]] = []
        for s in samples:
            st = stats.get(s["agent"])
            if not st or st["n"] < 5:
                continue  # need a real baseline before flagging
            std = math.sqrt(st["m2"] / st["n"])
            if std <= 0:
                continue
            z = (s["duration"] - st["mean"]) / std
            if z > z_threshold:
                anomalies.append({
                    "agent": s["agent"], "duration_s": round(s["duration"], 2),
                    "expected_s": round(st["mean"], 2), "z": round(z, 2),
                    "run_id": s.get("run_id"), "title": s.get("title"),
                })
        anomalies.sort(key=lambda a: -a["z"])
        return {"baselines": baselines, "anomalies": anomalies}
