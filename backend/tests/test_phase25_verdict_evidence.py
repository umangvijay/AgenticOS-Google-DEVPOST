"""Phase 25 §25.2 — the verdict must verify evidence EXISTS.

The Phase 25 audit reproduced a full 17-stage/22-journey report passing
with nonexistent evidence paths and 64-character non-hex hashes. These
regressions pin the repaired rules: retained files are opened, hashed,
parsed; journeys bind observed actions, runs, postconditions, and a
target identity that carries observed digests.
"""
import hashlib
import json
import uuid

from backend.tests.test_phase27_stage_schemas import _workload_body
import pytest

from scripts.qualify_release import (
    Qualifier, REQUIRED_STAGES, REQUIRED_JOURNEYS, MANIFEST_VERSION,
    journey_record, shell_evidence, pytest_evidence, ATTEMPTS,
)

CAND = "a" * 64
TARGET = "local-durable-postgres"


@pytest.fixture(autouse=True)
def evidence_root(monkeypatch, tmp_path):
    from scripts import qualify_release
    monkeypatch.setattr(qualify_release, "ATTEMPTS", tmp_path)


def make_evidence_dir(tmp_path):
    return tmp_path


def write_result(stage: str, ev_dir, counts=None, kind="pytest",
                 runner=None, ok=True) -> dict:
    """Write a genuine retained result file and return its record."""
    body = {"stage": stage, "ok": ok, "exit": 0 if ok else 1, "counts": counts or {}}
    if stage.startswith("product_e2e_"):
        body["command"] = ["test-fixture", stage]
    if stage == "migrations":
        body.update(db_profile="sqlite-fresh", migrations_ok=True, tables=1)
    if stage in ("live_inference_hosted", "pilot"):
        if stage == "live_inference_hosted":
            providers = {}
            for p in ("vertex", "bedrock", "azure"):
                trace = ev_dir / f"{stage}-fixture-trace-{p}.json"
                trace.write_text(json.dumps(
                    {"provider": p, "provider_request_id": f"fixture-request-{p}",
                     "dispatch": "metered", "verified": True}))
                providers[p] = {"result_file": str(trace),
                                "result_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                                "run_id": f"fixture-run-{p}", "model": "fixture-model",
                                "provider_request_id": f"fixture-request-{p}",
                                "checks": {c: True for c in
                                ("workflow", "tool_call", "usage", "cancellation", "rotation", "revocation")}}
            body["providers"] = providers
        else:
            # §28.1 complete fixture: 3 distinct participants, each session
            # trace agreeing with its receipt.
            sessions = []
            for i in range(3):
                trace = ev_dir / f"{stage}-fixture-trace-{i}.json"
                trace.write_text(json.dumps(
                    {"session_id": f"fixture-session-{i}",
                     "run_id": f"fixture-run-{i}", "verified_outcome": True}))
                sessions.append({
                    "result_file": str(trace),
                    "result_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
                    "session_id": f"fixture-session-{i}",
                    "participant_ref": f"fixture-participant-{i}",
                    "consent_ref": f"fixture-consent-{i}",
                    "run_id": f"fixture-run-{i}", "verified_outcome": True})
            body["sessions"] = sessions
    if stage == "performance":
        body.update(rate_limit={"ok": ok}, capacity={"ok": ok})
    if stage == "restore":
        body.update(subchecks={k: "ok" for k in ("dump", "restore", "integrity", "cleanup", "independent_app_trace")},
                    source_counts={"workflows": 1, "events": 1}, restored_counts={"workflows": 1, "events": 1})
    if stage == "target_deployment":
        body.update(image_digests={k: "sha256:" + "d" * 64 for k in ("api", "worker", "frontend")},
                    workflow={"submitted": True, "completed": True, "run_id": "test-run", "final_status": "COMPLETED"})
    if kind == "pytest":
        # Real junitxml so the parser can read it.
        cases = "".join(
            f'<testcase name="t{i}"/>' for i in range((counts or {}).get("passed", 1)))
        body_bytes = (f'<testsuites><testsuite name="{stage}">{cases}'
                      f"</testsuite></testsuites>").encode()
        path = ev_dir / f"{stage}.xml"
        path.write_bytes(body_bytes)
    elif stage == "performance":
        # §27.D1: the loadtest's structured outcome — the verdict now
        # validates its subchecks.
        body = {"workload": _workload_body(), "exit": 0, "rate_limit": {
                    "ok": True, "allowance": 12, "accepted_total": 12,
                    "ledger_count": 12, "attempts": 180},
                "capacity": {"ok": True, "skipped": False, "leases": 15},
                "ok": True}
        path = ev_dir / f"{stage}.json"
        path.write_text(json.dumps(body))
    else:
        if counts:
            body["counts"] = counts
            if runner:
                body["runner"] = runner
                # §29.2: vitest/playwright reports are retained as
                # stage/runner identity envelopes around the raw report.
                if runner == "vitest":
                    body = {"stage": stage, "runner": "vitest",
                            "command": ["npm", "test"], "exit": 0,
                            "report": {
                                "numPassedTests": counts.get("passed", 1),
                                "numFailedTests": 0, "numPendingTests": 0,
                                "numRuntimeErrorTestSuites": 0}}
                elif runner == "playwright":
                    body = {"stage": stage, "runner": "playwright",
                            "command": ["npm", "run", "test:e2e:production"],
                            "exit": 0,
                            "report": {
                                "stats": {"expected": counts.get("passed", 1),
                                          "unexpected": 0, "skipped": 0,
                                          "flaky": 0},
                                "errors": []}}
        path = ev_dir / f"{stage}.json"
        path.write_text(json.dumps(body))
    return {**({"image_digests": body["image_digests"]} if stage == "target_deployment" else {}),
            "kind": kind, "ok": True, "exit": 0, "counts": counts or {},
            **({"runner": runner} if runner else {}),
            "result_file": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def base_report(ev_dir):
    """A structurally VALID report with GENUINE evidence files."""
    stages = {}
    for name in REQUIRED_STAGES:
        if name in ("unit_contract", "fault_injection", "security", "infra_integration",
                    "live_inference_local", "flagship", "rl_training"):
            stages[name] = write_result(name, ev_dir,
                                        counts={"passed": 5}, kind="pytest")
        elif name in ("product_e2e_vitest",):
            stages[name] = write_result(name, ev_dir,
                                        counts={"passed": 90},
                                        kind="shell", runner="vitest")
        elif name in ("product_e2e_browser",):
            stages[name] = write_result(name, ev_dir,
                                        counts={"passed": 28},
                                        kind="shell", runner="playwright")
        else:
            stages[name] = write_result(name, ev_dir, kind="shell")
    journeys = {}
    from backend.tests._phase31_evidence import (
        journey_evidence_file, scenario_section,
    )
    for jid in REQUIRED_JOURNEYS:
        ev_file = journey_evidence_file(ev_dir, jid)
        journeys[jid] = journey_record(
            jid, candidate_sha256=CAND, target_id=TARGET,
            production_action=f"executed {jid} against the live stack",
            run_id=f"run-{jid}", expected_postcondition="COMPLETED",
            observed_result="COMPLETED with verified output",
            evidence=[{"result_file": str(ev_file),
                       "result_sha256": hashlib.sha256(
                           ev_file.read_bytes()).hexdigest()}])
        journeys[jid]["target_image_digests_sha256"] = "d" * 64
    journeys["J14"]["evidence"].append(stages["rl_training"])
    return {
        "scope": "full", "manifest_version": MANIFEST_VERSION,
        "stages": stages, "journeys": journeys,
        "scenarios": scenario_section(ev_dir),
        "candidate_identity": {"content_sha256": CAND},
        "target_identity": {"profile": TARGET, "image_digests_sha256": "d" * 64},
        "source_changed": False,
    }


def qualifier(report, evidence_dir=None):
    from scripts import qualify_release as _qr
    q = Qualifier.__new__(Qualifier)
    q.strict = False
    q.report = report
    # §29.2: the attempt directory scopes where evidence may live —
    # the fixtures monkeypatch ATTEMPTS to tmp_path and write their
    # retained files there.
    q.attempt_id = "test-attempt"
    q.attempt_dir = evidence_dir or _qr.ATTEMPTS
    return q


# ── the audit counterexample #2 ───────────────────────────────────────

def test_nonexistent_paths_and_nonhex_hashes_fail(tmp_path):
    stages = {}
    for name in REQUIRED_STAGES:
        stages[name] = {"kind": "pytest", "ok": True, "exit": 0,
                        "result_file": "/nonexistent/evidence.xml",
                        "result_sha256": "!" * 64,
                        "counts": {"passed": 3}}
    journeys = {j: {"journey_id": j, "ok": True,
                    "evidence": ["placeholder-text"],
                    "candidate_sha256": CAND, "target_id": TARGET,
                    "production_action": "x", "run_id": "r",
                    "expected_postcondition": "e",
                    "observed_result": "o"}
                for j in REQUIRED_JOURNEYS}
    q = Qualifier.__new__(Qualifier)
    q.strict = True
    q.attempt_id = "test-attempt"
    q.attempt_dir = tmp_path
    q.report = {"scope": "full", "manifest_version": MANIFEST_VERSION,
                "stages": stages, "journeys": journeys,
                "candidate_identity": {"content_sha256": CAND},
                "target_identity": {"profile": TARGET, "image_digests_sha256": "d" * 64},
                "source_changed": False}
    ok, problems = q.verdict()
    assert ok is False, "the audit counterexample must fail"
    assert any("nonexistent" in p or "not a hex" in p for p in problems)


def test_deleted_result_file_invalidates(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    ok, _ = qualifier(report).verdict()
    assert ok, "a genuine packet passes before deletion"
    # Delete one referenced file → invalidation.
    victim = report["stages"]["security"]["result_file"]
    (ev / "security.xml").unlink()
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("does not exist" in p for p in problems)


def test_modified_evidence_invalidates(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    target_file = ev / "unit_contract.xml"
    target_file.write_bytes(target_file.read_bytes() + b"<!--tampered-->")
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("bytes changed" in p for p in problems)


def test_nonhex_hash_invalidates(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["stages"]["migrations"]["result_sha256"] = "z" * 64
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("not a hex" in p for p in problems)


def test_recorded_counts_must_match_retained_result(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["stages"]["unit_contract"]["counts"] = {"passed": 9999}
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("contradicts" in p for p in problems)


# ── journey binding ───────────────────────────────────────────────────

def test_journey_without_observed_action_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[2]
    report["journeys"][jid]["production_action"] = ""
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("missing production_action" in p for p in problems)


def test_text_placeholder_evidence_is_not_evidence(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[4]
    report["journeys"][jid]["evidence"] = ["placeholder-text"]
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("not a verifiable record" in p for p in problems)


def test_source_reference_is_not_execution_evidence(tmp_path):
    """A path pointing at a TEST IMPLEMENTATION (outside the evidence
    root) documents how to run a check — it is not evidence of running."""
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[6]
    report["journeys"][jid]["evidence"] = [{
        "result_file": "backend/tests/test_phase24_standalone_dispatch.py",
        "result_sha256": hashlib.sha256(
            open("backend/tests/test_phase24_standalone_dispatch.py",
                 "rb").read()).hexdigest()}]
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("outside the approved evidence" in p for p in problems)


def test_wrong_target_with_same_label_fails(tmp_path):
    """A wrong deployment that shares the display label must still fail —
    the target identity carries observed digests."""
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["target_identity"]["image_digests_sha256"] = "d" * 64
    ok, _ = qualifier(report).verdict()
    assert ok, "the genuine packet with a digest-bound target passes"
    report["journeys"][sorted(report["journeys"])[8]]["target_id"] = TARGET
    # same label but a journey observed on a DIFFERENT digest: emulate by
    # giving the journey its own digest identity field
    report["journeys"][sorted(report["journeys"])[8]][
        "target_image_digests_sha256"] = "e" * 64
    assert not qualifier(report).verdict()[0]


def test_duplicate_json_journey_keys_fail():
    from scripts.qualify_release import unique_object
    with pytest.raises(ValueError, match="duplicate JSON key"):
        json.loads('{"J01": 1, "J01": 2}', object_pairs_hook=unique_object)
    assert json.loads('{"a":{"ok":true},"b":{"ok":true}}', object_pairs_hook=unique_object)


def test_mandatory_subcheck_skipped_with_exit_zero_fails(tmp_path):
    """The loadtest SKIP class of defect: a retained outcome whose own
    structured body says a mandatory subcheck was skipped must fail."""
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    outcome = ev / "performance.json"
    outcome.write_text(json.dumps({
        "workload": "admission", "rate_limit": {"ok": True},
        "capacity": {"ok": False, "skipped": True,
                     "reason": "capacity schema seed failed"},
        "ok": False}))
    report["stages"]["performance"] = {
        "kind": "shell", "ok": True, "exit": 0,
        "result_file": str(outcome),
        "result_sha256": hashlib.sha256(outcome.read_bytes()).hexdigest()}
    ok, problems = qualifier(report).verdict()
    # The verdict must reject: the stage flag claims ok but the retained
    # structured outcome records a skipped mandatory subcheck.
    assert not ok and any("performance" in problem for problem in problems)


# ── the genuine positive ──────────────────────────────────────────────

def test_genuine_packet_passes(tmp_path):
    ev = make_evidence_dir(tmp_path)
    ok, problems = qualifier(base_report(ev)).verdict()
    assert ok, problems


@pytest.mark.parametrize("stage", ["restore", "target_deployment"])
def test_stage_flag_cannot_hide_missing_semantic_receipt(tmp_path, stage):
    report = base_report(tmp_path)
    path = tmp_path / f"{stage}.json"
    path.write_text('{"ok": true}')
    report["stages"][stage]["result_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any(stage in p for p in problems)


def test_cloud_profile_cannot_be_qualified_by_local_compose(monkeypatch, tmp_path):
    monkeypatch.setenv("TARGET_PROFILE", "gcp-staging-postgres")
    q = qualifier(base_report(tmp_path))
    observed = {}
    # The producer API passes fields both positionally and as kwargs
    # (e.g. kind="blocked"); accept both shapes.
    def _record(stage, fields=None, **extra):
        observed[stage] = {**(fields or {}), **extra}
    q.record = _record
    q.s_target_deployment()
    assert observed["target_deployment"]["kind"] == "blocked"
    assert observed["target_deployment"]["ok"] is False
