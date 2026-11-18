"""Phase 28 §28.1 — the evidence gate rejects misleading producer receipts.

Named regressions from the Phase 28 audit: wrong-stage JUnit report,
duplicate JSON key, altered journey attachment, stale candidate,
missing journey, repeated participant under different session IDs, and
the pilot-plan participant denominator. Each must fail for its intended
reason; a complete fixture must prove the parser accepts the format.
"""
import hashlib
import json
from pathlib import Path

from backend.tests.test_phase27_stage_schemas import _workload_body
import pytest

from scripts.qualify_release import (
    Qualifier, REQUIRED_STAGES, REQUIRED_JOURNEYS, MANIFEST_VERSION,
    journey_record, parse_retained_counts, ATTEMPTS,
)

CAND = "a" * 64
OTHER_CAND = "b" * 64
TARGET = "local-durable-postgres"


@pytest.fixture(autouse=True)
def evidence_root(monkeypatch, tmp_path):
    from scripts import qualify_release
    monkeypatch.setattr(qualify_release, "ATTEMPTS", tmp_path)


def write_junit(stage: str, ev_dir, suite_name=None, passed=3) -> str:
    """A retained pytest JUnit report whose root suite name is the stage
    identity written by the producing run."""
    name = suite_name or stage
    cases = "".join(f'<testcase name="t{i}"/>' for i in range(passed))
    path = ev_dir / f"{stage}.xml"
    path.write_bytes(
        f'<testsuites><testsuite name="{name}">{cases}</testsuite>'
        f"</testsuites>".encode())
    return path


def make_stage_records(ev_dir):
    stages = {}
    for name in REQUIRED_STAGES:
        if name in ("unit_contract", "fault_injection", "security",
                    "infra_integration", "live_inference_local", "flagship",
                    "rl_training"):
            path = write_junit(name, ev_dir)
            stages[name] = {"kind": "pytest", "ok": True, "exit": 0,
                            "counts": {"passed": 3},
                            "result_file": str(path),
                            "result_sha256": hashlib.sha256(
                                path.read_bytes()).hexdigest()}
        elif name == "product_e2e_vitest":
            # §29.2: the producer wraps the raw vitest report in a
            # stage/runner identity envelope.
            stages[name] = _shell(ev_dir, name, {
                "stage": name, "runner": "vitest",
                "command": ["npm", "test"], "exit": 0,
                "report": {
                    "numPassedTests": 90, "numFailedTests": 0,
                    "numPendingTests": 0, "numRuntimeErrorTestSuites": 0}},
                extra={"counts": {"passed": 90}})
        elif name == "product_e2e_browser":
            stages[name] = _shell(ev_dir, name, {
                "stage": name, "runner": "playwright",
                "command": ["npm", "run", "test:e2e:production"], "exit": 0,
                "report": {
                    "stats": {"expected": 28, "unexpected": 0, "skipped": 0,
                              "flaky": 0}, "errors": []}},
                extra={"counts": {"passed": 28}})
        elif name in ("product_e2e_typecheck", "product_e2e_lint",
                      "product_e2e_build"):
            stages[name] = _shell(ev_dir, name, {
                "stage": name, "ok": True, "exit": 0,
                "command": ["npm", "run", name.split("_", 2)[2]]})
        elif name == "performance":
            stages[name] = _shell(ev_dir, name, {
                "workload": _workload_body(), "exit": 0,
                "rate_limit": {"ok": True, "allowance": 12,
                               "accepted_total": 12, "ledger_count": 12},
                "capacity": {"ok": True, "skipped": False},
                "ok": True})
        elif name == "restore":
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0,
                "subchecks": {k: "ok" for k in (
                    "dump", "restore", "integrity", "cleanup",
                    "independent_app_trace")},
                "source_counts": {"workflows": 1, "events": 1},
                "restored_counts": {"workflows": 1, "events": 1}})
        elif name == "target_deployment":
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0,
                "image_digests": {k: "sha256:" + "d" * 64
                                  for k in ("api", "worker", "frontend")},
                "workflow": {"submitted": True, "completed": True,
                             "run_id": "run-x", "final_status": "COMPLETED"}},
                extra={"image_digests": {k: "sha256:" + "d" * 64
                                         for k in ("api", "worker", "frontend")}})
        elif name == "live_inference_hosted":
            providers = {}
            for provider in ("vertex", "bedrock", "azure"):
                trace = ev_dir / f"hosted-{provider}.json"
                trace.write_text(json.dumps(
                    {"provider": provider,
                     "provider_request_id": f"pr-{provider}",
                     "dispatch": "metered", "verified": True}))
                providers[provider] = {
                    "run_id": f"r-{provider}", "model": "m",
                    "provider_request_id": f"pr-{provider}",
                    "checks": {"workflow": True, "tool_call": True,
                               "usage": True, "cancellation": True,
                               "rotation": True, "revocation": True},
                    "result_file": str(trace),
                    "result_sha256": hashlib.sha256(
                        trace.read_bytes()).hexdigest()}
            stages[name] = _shell(ev_dir, name,
                                  {"exit": 0, "ok": True,
                                   "providers": providers})
        elif name == "pilot":
            sessions = []
            for i in range(3):
                trace = ev_dir / f"pilot-session-{i}.json"
                trace.write_text(json.dumps(
                    {"session_id": f"s-{i}", "run_id": f"r-{i}",
                     "verified_outcome": True}))
                sessions.append({
                    "session_id": f"s-{i}",
                    "participant_ref": f"p-{i}",
                    "consent_ref": f"c-{i}",
                    "run_id": f"r-{i}",
                    "verified_outcome": True,
                    "result_file": str(trace),
                    "result_sha256": hashlib.sha256(
                        trace.read_bytes()).hexdigest()})
            stages[name] = _shell(ev_dir, name,
                                  {"ok": True, "exit": 0,
                                   "sessions": sessions})
        elif name == "migrations":
            stages[name] = _shell(ev_dir, name, {
                "db_profile": "sqlite-fresh", "migrations_ok": True,
                "tables": 42, "ok": True, "exit": 0})
        else:
            stages[name] = _shell(ev_dir, name, {"ok": True, "exit": 0})
    return stages


def _shell(ev_dir, name, body, extra=None):
    path = ev_dir / f"{name}.json"
    path.write_text(json.dumps(body))
    record = {"kind": "shell", "ok": True, "exit": 0,
              "result_file": str(path),
              "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    record.update(extra or {})
    return record


def base_report(ev_dir):
    from backend.tests._phase31_evidence import (
        journey_evidence_file, scenario_section,
    )
    stages = make_stage_records(ev_dir)
    journeys = {}
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
        "target_identity": {"profile": TARGET,
                            "image_digests_sha256": "d" * 64},
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


# ── the named regressions ─────────────────────────────────────────────

def test_wrong_stage_junit_report_fails(tmp_path):
    """A JUnit report produced for one stage cannot be recorded under
    another stage's record — the root suite name binds the identity."""
    ev = tmp_path
    # fault_injection's report recorded as the security stage.
    path = write_junit("security", ev, suite_name="fault_injection")
    record = {"kind": "pytest", "ok": True, "exit": 0,
              "counts": {"passed": 3}, "result_file": str(path),
              "result_sha256": hashlib.sha256(
                  path.read_bytes()).hexdigest()}
    problems = parse_retained_counts("security", record, ev)
    assert any("wrong-stage evidence" in p for p in problems), problems


def test_stage_named_junit_is_accepted(tmp_path):
    ev = tmp_path
    path = write_junit("security", ev)
    record = {"kind": "pytest", "ok": True, "exit": 0,
              "counts": {"passed": 3}, "result_file": str(path),
              "result_sha256": hashlib.sha256(
                  path.read_bytes()).hexdigest()}
    assert parse_retained_counts("security", record, ev) == []


def test_duplicate_json_key_in_retained_result_fails(tmp_path):
    ev = tmp_path
    path = ev / "performance.json"
    # NOTE: written raw — two "ok" keys in one object.
    path.write_text('{"ok": true, "ok": false, "exit": 0, '
                    '"rate_limit": {"ok": true, "allowance": 12, '
                    '"accepted_total": 12}, "capacity": {"ok": true}}')
    record = {"kind": "shell", "ok": True, "exit": 0,
              "result_file": str(path),
              "result_sha256": hashlib.sha256(
                  path.read_bytes()).hexdigest()}
    problems = parse_retained_counts("performance", record, ev)
    assert any("duplicate JSON key" in p for p in problems), problems


def test_altered_journey_attachment_fails(tmp_path):
    ev = tmp_path
    report = base_report(ev)
    ok, _ = qualifier(report).verdict()
    assert ok, "the genuine packet passes before alteration"
    jid = sorted(report["journeys"])[3]
    attachment = Path(report["journeys"][jid]["evidence"][0]["result_file"])
    attachment.write_bytes(
        attachment.read_bytes() + b'{"tampered": true}')
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("bytes changed" in p for p in problems)


def test_stale_candidate_evidence_fails(tmp_path):
    ev = tmp_path
    report = base_report(ev)
    jid = sorted(report["journeys"])[5]
    report["journeys"][jid]["candidate_sha256"] = OTHER_CAND
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("different candidate" in p for p in problems)


def test_missing_journey_fails(tmp_path):
    ev = tmp_path
    report = base_report(ev)
    victim = sorted(report["journeys"])[7]
    del report["journeys"][victim]
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any(f"journey {victim}" in p and "missing" in p
               for p in problems)


def test_repeated_participant_under_different_session_ids_fails(tmp_path):
    ev = tmp_path
    sessions = []
    for i in range(3):
        trace = ev / f"pilot-dup-{i}.json"
        trace.write_text(json.dumps(
            {"session_id": f"s-{i}", "run_id": f"r-{i}",
             "verified_outcome": True}))
        sessions.append({
            "session_id": f"s-{i}", "participant_ref": "same-person",
            "consent_ref": f"c-{i}", "run_id": f"r-{i}",
            "verified_outcome": True, "result_file": str(trace),
            "result_sha256": hashlib.sha256(
                trace.read_bytes()).hexdigest()})
    record = _shell(ev, "pilot", {"ok": True, "exit": 0,
                                  "sessions": sessions})
    problems = parse_retained_counts("pilot", record, ev)
    assert any("multiple session IDs" in p for p in problems), problems


def test_pilot_requires_three_to_five_distinct_participants(tmp_path):
    ev = tmp_path
    trace = ev / "pilot-one.json"
    trace.write_text(json.dumps(
        {"session_id": "s-0", "run_id": "r-0", "verified_outcome": True}))
    sessions = [{"session_id": "s-0", "participant_ref": "p-0",
                 "consent_ref": "c-0", "run_id": "r-0",
                 "verified_outcome": True, "result_file": str(trace),
                 "result_sha256": hashlib.sha256(
                     trace.read_bytes()).hexdigest()}]
    record = _shell(ev, "pilot", {"ok": True, "exit": 0,
                                  "sessions": sessions})
    problems = parse_retained_counts("pilot", record, ev)
    assert any("3–5 distinct participants" in p for p in problems), problems


def test_pilot_session_trace_must_agree_with_receipt(tmp_path):
    ev = tmp_path
    sessions = []
    for i in range(3):
        trace = ev / f"pilot-mismatch-{i}.json"
        # The trace records a DIFFERENT session id than the receipt.
        trace.write_text(json.dumps(
            {"session_id": f"other-{i}", "run_id": f"r-{i}",
             "verified_outcome": True}))
        sessions.append({
            "session_id": f"s-{i}", "participant_ref": f"p-{i}",
            "consent_ref": f"c-{i}", "run_id": f"r-{i}",
            "verified_outcome": True, "result_file": str(trace),
            "result_sha256": hashlib.sha256(
                trace.read_bytes()).hexdigest()})
    record = _shell(ev, "pilot", {"ok": True, "exit": 0,
                                  "sessions": sessions})
    problems = parse_retained_counts("pilot", record, ev)
    assert any("contradicts the receipt" in p for p in problems), problems


def test_unsuccessful_pilot_observations_are_retained_not_hidden(tmp_path):
    """28.12: a qualifying pilot record may RETAIN unsuccessful
    observations without failing — they are evaluated under the declared
    pilot criteria, never silently dropped or counted as successes."""
    ev = tmp_path
    sessions = []
    for i in range(3):
        trace = ev / f"pilot-ok-{i}.json"
        trace.write_text(json.dumps(
            {"session_id": f"s-{i}", "run_id": f"r-{i}",
             "verified_outcome": True}))
        sessions.append({
            "session_id": f"s-{i}", "participant_ref": f"p-{i}",
            "consent_ref": f"c-{i}", "run_id": f"r-{i}",
            "verified_outcome": True, "result_file": str(trace),
            "result_sha256": hashlib.sha256(
                trace.read_bytes()).hexdigest()})
    failed_trace = ev / "pilot-failed.json"
    failed_trace.write_text(json.dumps(
        {"session_id": "s-failed", "run_id": "r-failed",
         "verified_outcome": False}))
    body = {"ok": True, "exit": 0, "sessions": sessions,
            "unsuccessful_sessions": [{
                "session_id": "s-failed", "run_id": "r-failed",
                "result_file": str(failed_trace),
                "result_sha256": hashlib.sha256(
                    failed_trace.read_bytes()).hexdigest()}]}
    record = _shell(ev, "pilot", body)
    assert parse_retained_counts("pilot", record, ev) == []


# ── the genuine complete packet ───────────────────────────────────────

def test_genuine_complete_packet_passes(tmp_path):
    ok, problems = qualifier(base_report(tmp_path)).verdict()
    assert ok, problems


def test_tag_only_release_manifest_is_rejected(tmp_path):
    """28.10: a target_deployment receipt whose image identities are TAGS
    (or missing) cannot pass — the qualifier requires sha256 digest
    identities for every participating component."""
    ev = tmp_path
    record = _shell(ev, "target_deployment", {
        "ok": True, "exit": 0,
        "image_digests": {"api": "agentos-api:latest",          # tag, not digest
                          "worker": "sha256:" + "d" * 64,
                          "frontend": "sha256:" + "d" * 64},
        "workflow": {"submitted": True, "completed": True,
                     "run_id": "run-x", "final_status": "COMPLETED"}},
        extra={"image_digests": {"api": "agentos-api:latest",
                                 "worker": "sha256:" + "d" * 64,
                                 "frontend": "sha256:" + "d" * 64}})
    problems = parse_retained_counts("target_deployment", record, ev)
    assert any("immutable deployed image identities" in p
               for p in problems), problems


def test_restore_drill_fails_when_the_app_trace_is_missing_a_check(tmp_path):
    """28.11: injecting a missing key/artifact shows up as a failed
    independent_app_trace subcheck — the stage must fail, never fold the
    app trace into a database-count pass."""
    ev = tmp_path
    subchecks = {"dump": "ok", "restore": "ok", "integrity": "ok",
                 "cleanup": "ok",
                 "independent_app_trace": "failed (checks 13/14: "
                                          "restored_vault_credential_challenge)"}
    # The stage flag claims ok — the retained body's failed subcheck
    # must still reject it (a flag cannot hide an injected failure).
    record = _shell(ev, "restore", {
        "ok": True, "exit": 0, "subchecks": subchecks,
        "source_counts": {"workflows": 3, "events": 9},
        "restored_counts": {"workflows": 3, "events": 9}})
    problems = parse_retained_counts("restore", record, ev)
    assert problems, "a drill that failed its app trace cannot pass"
    assert any("missing or failed" in p for p in problems), problems


def test_restore_counts_mismatch_fails(tmp_path):
    ev = tmp_path
    record = _shell(ev, "restore", {
        "ok": True, "exit": 0,
        "subchecks": {k: "ok" for k in ("dump", "restore", "integrity",
                                        "cleanup", "independent_app_trace")},
        "source_counts": {"workflows": 5, "events": 11},
        "restored_counts": {"workflows": 4, "events": 11}})
    problems = parse_retained_counts("restore", record, ev)
    assert any("do not match" in p for p in problems), problems


def _pilot_receipt(ev, sessions, unsuccessful=None):
    return _shell(ev, "pilot", {
        "ok": True, "exit": 0, "sessions": sessions,
        **({"unsuccessful_sessions": unsuccessful} if unsuccessful else {})})


def test_second_altered_unsuccessful_attachment_fails(tmp_path):
    """28.1: EVERY retained unsuccessful observation is checked — a valid
    first attachment cannot carry an altered second one."""
    ev = tmp_path
    sessions = []
    for i in range(3):
        trace = ev / f"pilot-s-{i}.json"
        trace.write_text(json.dumps(
            {"session_id": f"s-{i}", "run_id": f"r-{i}",
             "verified_outcome": True}))
        sessions.append({
            "session_id": f"s-{i}", "participant_ref": f"p-{i}",
            "consent_ref": f"c-{i}", "run_id": f"r-{i}",
            "verified_outcome": True, "result_file": str(trace),
            "result_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()})
    failed = []
    for i in range(2):
        trace = ev / f"pilot-f-{i}.json"
        trace.write_text(json.dumps(
            {"session_id": f"f-{i}", "run_id": f"rf-{i}",
             "verified_outcome": False}))
        failed.append({
            "session_id": f"f-{i}", "participant_ref": f"q-{i}",
            "consent_ref": f"qc-{i}", "run_id": f"rf-{i}",
            "verified_outcome": False, "result_file": str(trace),
            "result_sha256": hashlib.sha256(trace.read_bytes()).hexdigest()})
    record = _pilot_receipt(ev, sessions, failed)
    assert parse_retained_counts("pilot", record, ev) == [], \
        "a fully intact record passes"
    # Alter the SECOND unsuccessful attachment after its hash was taken.
    second = Path(failed[1]["result_file"])
    second.write_bytes(second.read_bytes() + b'{"tampered": true}')
    problems = parse_retained_counts("pilot", record, ev)
    assert any("bytes changed" in p for p in problems), problems


def test_same_run_id_as_evidence_on_two_candidates_fails(tmp_path):
    """A run receipt bound to one candidate cannot be replayed as journey
    evidence for a different candidate or target."""
    ev = tmp_path
    report = base_report(ev)
    ok, _ = qualifier(report).verdict()
    assert ok, "genuine packet passes first"
    jid = sorted(report["journeys"])[2]
    ev_file = Path(report["journeys"][jid]["evidence"][0]["result_file"])
    # Same run receipt bytes, but recorded for a DIFFERENT candidate.
    report["journeys"][jid]["production_action"] = (
        f"executed {jid} on run run-{jid}")
    report["journeys"][jid]["run_id"] = f"run-{jid}"
    report["journeys"][jid]["candidate_sha256"] = "b" * 64
    ok, problems = qualifier(report).verdict()
    assert not ok and any("different candidate" in p for p in problems)
    # Restored candidate, but a DIFFERENT target.
    report["journeys"][jid]["candidate_sha256"] = CAND
    report["journeys"][jid]["target_id"] = "other-cloud-target"
    ok, problems = qualifier(report).verdict()
    assert not ok and any("different target" in p for p in problems)
