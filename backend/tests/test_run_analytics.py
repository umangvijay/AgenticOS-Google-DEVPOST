"""RunAnalytics regression model tests — baselines + z-score anomalies."""
from backend.services.run_analytics import RunAnalytics


def _ts(offset):
    from datetime import datetime, timedelta, timezone
    return (datetime(2026, 10, 19, tzinfo=timezone.utc) + timedelta(seconds=offset)).isoformat()


def _run(agent, start, duration, run_id="r"):
    return {"run_id": run_id, "created_at": _ts(start), "tasks": [
        {"agent": agent, "status": "COMPLETED", "completed_at": _ts(start + duration), "title": "t"}]}


def test_baselines_from_history():
    ra = RunAnalytics(None)
    runs = [_run("core.health", i * 100, 2 + i * 0.1) for i in range(6)]
    res = ra.analyze(runs)
    b = {x["agent"]: x for x in res["baselines"]}
    assert b["core.health"]["samples"] == 6
    assert 2 <= b["core.health"]["expected_s"] <= 4


def test_anomaly_flags_slow_outlier():
    ra = RunAnalytics(None)
    runs = [_run("core.health", i * 100, 2.0) for i in range(8)]
    runs.append(_run("core.health", 900, 120))  # the outlier
    res = ra.analyze(runs)
    agents = {a["agent"] for a in res["anomalies"]}
    assert "core.health" in agents


def test_small_samples_are_conservative():
    ra = RunAnalytics(None)
    runs = [_run("core.health", 0, 2.0), _run("core.health", 100, 90)]
    res = ra.analyze(runs)
    assert res["anomalies"] == []  # n < 5: no baseline, no flags


def test_failed_and_missing_tasks_skipped():
    ra = RunAnalytics(None)
    runs = [{"run_id": "r", "created_at": _ts(0), "tasks": [
        {"agent": "core.health", "status": "FAILED", "completed_at": _ts(5)}]}]
    res = ra.analyze(runs)
    assert res["baselines"] == [] and res["anomalies"] == []
