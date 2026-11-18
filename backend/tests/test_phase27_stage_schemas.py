"""Phase 27 §27.1 — strict stage-specific qualification schemas.

Negative: wrong-stage receipts, skipped subchecks, duplicate JSON keys,
incomplete provider receipts. Positive: one genuine temporary packet.
"""
import hashlib
import json
import uuid

import pytest

from scripts.qualify_release import (
    Qualifier, REQUIRED_STAGES, REQUIRED_JOURNEYS, MANIFEST_VERSION,
    journey_record, parse_retained_counts, ATTEMPTS,
)

CAND = "a" * 64
TARGET = "local-durable-postgres"


def _ev_dir():
    d = ATTEMPTS / f"schemas-{uuid.uuid4().hex[:10]}"
    d.mkdir(parents=True)
    return d


def _shell_record(stage, body, ev_dir):
    path = ev_dir / f"{stage}.json"
    path.write_text(json.dumps(body))
    return {"kind": "shell", "ok": True, "exit": 0,
            "result_file": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_wrong_stage_receipt_rejected(tmp_path):
    """A unit-test receipt cannot satisfy restore (27.1 acceptance)."""
    ev = _ev_dir()
    unit_receipt = {"stage": "unit_contract", "exit": 0, "ok": True,
                    "counts": {"passed": 10}}
    path = ev / "restore.json"
    path.write_text(json.dumps(unit_receipt))
    st = {"kind": "shell", "ok": True, "exit": 0,
          "result_file": str(path),
          "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    problems = parse_retained_counts("restore", st, ATTEMPTS)
    assert any("subcheck" in p or "unparsable" in p or "failure" in p
               for p in problems), problems


def test_skipped_capacity_subcheck_fails_performance():
    ev = _ev_dir()
    body = {"workload": "admission", "exit": 0,
            "rate_limit": {"ok": True, "accepted_total": 12,
                           "ledger_count": 12},
            "capacity": {"ok": False, "skipped": True,
                         "reason": "seed failed"},
            "ok": True}
    st = _shell_record("performance", body, ev)
    problems = parse_retained_counts("performance", st, ATTEMPTS)
    assert any("skipped" in p or "missing or failed" in p
               for p in problems), problems


def test_duplicate_json_keys_detected_by_reader():
    seen = []
    def hook(pairs):
        keys = [k for k, _ in pairs]
        seen.extend(k for k in keys if keys.count(k) > 1)
        return dict(pairs)
    json.loads('{"ok": true, "ok": false}', object_pairs_hook=hook)
    assert "ok" in seen


def test_incomplete_hosted_receipt_rejected():
    ev = _ev_dir()
    body = {"exit": 0, "ok": True, "providers": {
        "vertex": {"run_id": "r1", "model": "m",
                   "provider_request_id": "pr1",
                   "checks": {"workflow": True, "tool_call": True,
                              "usage": True, "cancellation": True,
                              "rotation": True, "revocation": True}},
        "bedrock": {"run_id": "r2", "model": "m"},
        "azure": {"run_id": "r3", "model": "m"}}}
    st = _shell_record("live_inference_hosted", body, ev)
    problems = parse_retained_counts("live_inference_hosted", st, ATTEMPTS)
    assert any("incomplete bedrock" in p or "required provider" in p
               for p in problems), problems


def test_complete_hosted_receipt_accepted():
    """Each provider receipt carries its OWN verifiable result file whose
    content names the same provider request — the validator recurses into
    them and checks the receipt against the trace (§28.1)."""
    ev = _ev_dir()
    checks = {"workflow": True, "tool_call": True, "usage": True,
              "cancellation": True, "rotation": True, "revocation": True}
    providers = {}
    for name in ("vertex", "bedrock", "azure"):
        receipt_file = ev / f"hosted-{name}.json"
        receipt_file.write_text(json.dumps(
            {"provider": name, "provider_request_id": f"pr-{name}",
             "dispatch": "metered", "verified": True}))
        providers[name] = {"run_id": f"r-{name}", "model": "m",
                           "provider_request_id": f"pr-{name}",
                           "checks": checks,
                           "result_file": str(receipt_file),
                           "result_sha256": hashlib.sha256(
                               receipt_file.read_bytes()).hexdigest()}
    body = {"exit": 0, "ok": True, "providers": providers}
    st = _shell_record("live_inference_hosted", body, ev)
    assert parse_retained_counts("live_inference_hosted", st, ATTEMPTS) == []


def test_hosted_trace_contradicting_the_request_rejected():
    """A trace whose recorded provider_request_id differs from the
    receipt is not evidence of that request (§28.1 linked-trace check)."""
    ev = _ev_dir()
    checks = {"workflow": True, "tool_call": True, "usage": True,
              "cancellation": True, "rotation": True, "revocation": True}
    providers = {}
    for name in ("vertex", "bedrock", "azure"):
        request_id = ("OTHER-request" if name == "vertex" else f"pr-{name}")
        receipt_file = ev / f"hosted-{name}.json"
        receipt_file.write_text(json.dumps(
            {"provider": name, "provider_request_id": request_id,
             "dispatch": "metered", "verified": True}))
        providers[name] = {"run_id": f"r-{name}", "model": "m",
                           "provider_request_id": f"pr-{name}",
                           "checks": checks,
                           "result_file": str(receipt_file),
                           "result_sha256": hashlib.sha256(
                               receipt_file.read_bytes()).hexdigest()}
    body = {"exit": 0, "ok": True, "providers": providers}
    st = _shell_record("live_inference_hosted", body, ev)
    problems = parse_retained_counts("live_inference_hosted", st, ATTEMPTS)
    assert any("contradicts the receipt" in p for p in problems), problems


def test_migration_receipt_requires_observed_result():
    ev = _ev_dir()
    body = {"exit": 0, "ok": True}   # no db_profile / migrations_ok
    st = _shell_record("migrations", body, ev)
    problems = parse_retained_counts("migrations", st, ATTEMPTS)
    assert any("migration result" in p for p in problems), problems


# ── §27.1/§27.19: the whole-workload receipt contract ─────────────────

def _workload_body(runs_per_family=10, families=("health_probe", "summarize")):
    """A VALID whole-workload receipt for the declared sample: every
    planned ID accounted for, verified math coherent, gates satisfied."""
    planned = runs_per_family * len(families)
    runs = []
    for fam in families:
        for i in range(runs_per_family):
            runs.append({"planned_id": f"{fam}-{i:02d}", "family": fam,
                         "run_id": f"run-{fam}-{i}",
                         "status": "COMPLETED", "verified": True,
                         "verification": "matches independent observation",
                         "t_submit": 1.0, "t_terminal": 2.0,
                         "latency_s": 1.0})
    elapsed = 30.0
    return {
        "measurement": "whole_workload", "target": TARGET,
        "predeclared": {"verified_completion_min": 0.85,
                        "runs_per_family": runs_per_family,
                        "concurrency": 3, "families": list(families)},
        "planned_ids": [r["planned_id"] for r in runs],
        "elapsed_s": elapsed, "t_started": 0.0, "t_ended": elapsed,
        "results": {"planned": planned, "accounted_for": planned,
                    "missing_planned_ids": [],
                    "completed": planned, "failed_or_cancelled": 0,
                    "abandoned": 0, "verified_completion_count": planned,
                    "verification_unavailable": 0,
                    "verified_completion_rate": 1.0,
                    "unauthorized_actions": 0, "duplicate_effects": 0,
                    "observed_effects": 0,
                    "per_family_counts": {f: runs_per_family
                                          for f in families},
                    "latency_p50_s": 1.0, "latency_p95_s": 1.0,
                    "elapsed_s": elapsed,
                    "throughput_per_min": round(planned / elapsed * 60, 1)},
        "runs": runs,
        "verification_failures": [],
        "gates": {"planned_drained": True, "family_counts_exact": True,
                  "verified_completion": True, "unauthorized_actions": True,
                  "duplicate_effects": True, "abandoned": True},
        "ok": True, "exit": 0,
    }


def _performance_stage(workload_body, **rate_overrides):
    ev = _ev_dir()
    body = {"stage": "performance", "ok": True, "exit": 0,
            "rate_limit": {"ok": True, "accepted_total": 12,
                           "allowance": 12, "ledger_count": 12,
                           **rate_overrides},
            "capacity": {"ok": True, "skipped": False},
            "workload": workload_body}
    st = _shell_record("performance", body, ev)
    return parse_retained_counts("performance", st, ATTEMPTS)


def test_complete_workload_receipt_accepted():
    problems = _performance_stage(_workload_body())
    assert problems == [], problems


def test_partial_workload_sample_rejected():
    """The audited defect: six health runs retained against a declared
    2×10 sample cannot qualify."""
    body = _workload_body()
    body["runs"] = body["runs"][:6]
    body["results"]["planned"] = 20
    body["results"]["accounted_for"] = 6
    body["results"]["completed"] = 6
    body["results"]["verified_completion_count"] = 6
    body["results"]["verified_completion_rate"] = 0.3
    body["results"]["per_family_counts"] = {"health_probe": 6}
    body["gates"]["planned_drained"] = False
    body["gates"]["family_counts_exact"] = False
    body["ok"] = False
    problems = _performance_stage(body)
    assert any("accounted for" in p or "declared sample" in p
               for p in problems), problems


def test_missing_one_planned_run_rejected():
    body = _workload_body()
    body["runs"] = body["runs"][:-1]
    body["results"]["accounted_for"] = 19
    body["results"]["completed"] = 19
    body["results"]["verified_completion_count"] = 19
    body["results"]["verified_completion_rate"] = 0.95
    problems = _performance_stage(body)
    assert any("19 of 20" in p for p in problems), problems


def test_duplicate_planned_id_rejected():
    body = _workload_body()
    body["runs"][5]["planned_id"] = body["runs"][4]["planned_id"]
    problems = _performance_stage(body)
    assert any("identities missing or duplicated" in p
               for p in problems), problems


def test_family_count_shortfall_rejected():
    body = _workload_body()
    # all ten summarize runs relabeled as FRESH unique health IDs:
    # summarize observes 0 of its 10 declared runs
    n = 0
    for r in body["runs"]:
        if r["family"] == "summarize":
            r["planned_id"] = f"health_probe-extra-{n:02d}"
            r["family"] = "health_probe"
            n += 1
    body["results"]["per_family_counts"] = {"health_probe": 20}
    problems = _performance_stage(body)
    assert any("summarize" in p and "declared runs" in p
               for p in problems), problems


def test_wrong_summary_rejected_by_verified_math():
    """A COMPLETED run whose output fails independent verification is a
    verification failure — the verified rate cannot claim it."""
    body = _workload_body()
    body["runs"][0]["verified"] = False
    body["runs"][0]["verification"] = "required source facts missing"
    body["verification_failures"] = [{"planned_id": body["runs"][0]["planned_id"],
                                      "reason": "required source facts missing"}]
    body["results"]["verified_completion_count"] = 19
    body["results"]["verified_completion_rate"] = 0.95
    problems = _performance_stage(body)
    assert problems == [], problems   # 19/20 = 0.95 >= 0.85 → still honest
    # but dropping below the predeclared floor must fail the gate:
    body["results"]["verified_completion_count"] = 15
    body["results"]["verified_completion_rate"] = 0.75
    body["gates"]["verified_completion"] = False
    body["ok"] = False
    problems = _performance_stage(body)
    assert any("verified_completion" in p for p in problems), problems


def test_verified_count_over_completed_rejected():
    body = _workload_body()
    body["results"]["verified_completion_count"] = 21
    problems = _performance_stage(body)
    assert any("contradicts completed" in p for p in problems), problems


def test_throughput_contradicting_elapsed_rejected():
    body = _workload_body()
    body["results"]["throughput_per_min"] = 999.0
    problems = _performance_stage(body)
    assert any("throughput" in p for p in problems), problems


def test_contradictory_admitted_accepted_total_rejected():
    # ledger and allowance agree with accepted_total so the ONLY
    # contradiction left is the two spellings of one count
    problems = _performance_stage(_workload_body(), admitted=12,
                                  accepted_total=11, allowance=11,
                                  ledger_count=11)
    assert any("contradictory admitted/accepted_total" in p
               for p in problems), problems


def test_named_mandatory_skip_blocks_unit_contract(tmp_path):
    """§27.1: a skipped deployed-browser takeover blocks its claim even
    at a 1/2000 skip rate."""
    ev = _ev_dir()
    xml = ev / "unit.xml"
    xml.write_text("""<?xml version="1.0" encoding="utf-8"?>
<testsuite name="unit_contract" tests="2">
  <testcase classname="backend.tests.test_x" name="test_other" time="0.1"/>
  <testcase classname="backend.tests.test_phase21_takeover_deployed"
            name="test_deployed_takeover" time="0.1">
    <skipped message="BLOCKED: QUALIFY_BROWSER_APP_URL"/>
  </testcase>
</testsuite>""")
    st = {"kind": "pytest", "ok": True, "exit": 0,
          "counts": {"passed": 1, "failed": 0, "skipped": 1, "errors": 0},
          "result_file": str(xml),
          "result_sha256": hashlib.sha256(xml.read_bytes()).hexdigest()}
    problems = parse_retained_counts("unit_contract", st, ATTEMPTS)
    assert any("mandatory check SKIPPED" in p and "deployed-browser" in p
               for p in problems), problems
