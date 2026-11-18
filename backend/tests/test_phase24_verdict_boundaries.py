"""Phase 24 §24.3 — verdict boundary regressions.

Phase 25 upgrade: these tests now build GENUINE evidence files through
the shared helpers in test_phase25_verdict_evidence.py — the repaired
verdict verifies retained bytes, so in-memory records are no longer
valid fixtures.
"""
import hashlib
import json
import uuid

import pytest

from scripts.qualify_release import (
    Qualifier, REQUIRED_STAGES, REQUIRED_JOURNEYS, MANIFEST_VERSION,
    journey_record,
)
from backend.tests.test_phase25_verdict_evidence import (
    make_evidence_dir, write_result, base_report, evidence_root, CAND, TARGET,
)


def qualifier(report):
    q = Qualifier.__new__(Qualifier)
    q.strict = False
    q.report = report
    return q



# ── the exact audit counterexample ────────────────────────────────────

def test_subset_with_17_bare_shell_successes_cannot_pass():
    q = Qualifier.__new__(Qualifier)
    q.strict = True
    q.report = {"scope": "subset", "stages": {
        name: {"kind": "shell", "ok": True, "exit": 0}
        for name in REQUIRED_STAGES}}
    ok, problems = q.verdict()
    assert ok is False
    assert problems


def test_missing_scope_field_cannot_pass(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    del report["scope"]
    assert not qualifier(report).verdict()[0]


# ── journey evidence ──────────────────────────────────────────────────

def test_all_22_journeys_required(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["journeys"].pop(sorted(report["journeys"])[-1])
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("journey" in p for p in problems)


def test_21_of_22_journeys_present_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["journeys"] = {j: report["journeys"][j]
                          for j in list(report["journeys"])[:21]}
    assert not qualifier(report).verdict()[0]


def test_duplicate_journey_ids_fail(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    first = sorted(report["journeys"])[0]
    report["journeys"][first] = [report["journeys"][first],
                                 report["journeys"][first]]
    assert not qualifier(report).verdict()[0]


def test_journey_evidence_from_wrong_candidate_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[3]
    report["journeys"][jid]["candidate_sha256"] = "f" * 64
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any(jid in p for p in problems)


def test_journey_without_evidence_records_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[5]
    report["journeys"][jid]["evidence"] = []
    assert not qualifier(report).verdict()[0]


def test_journey_not_ok_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    jid = sorted(report["journeys"])[7]
    report["journeys"][jid]["ok"] = False
    assert not qualifier(report).verdict()[0]


# ── candidate / target / manifest identity ────────────────────────────

def test_missing_candidate_identity_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report.pop("candidate_identity")
    assert not qualifier(report).verdict()[0]


def test_missing_target_identity_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report.pop("target_identity")
    assert not qualifier(report).verdict()[0]


def test_wrong_target_profile_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["target_identity"]["profile"] = "sqlite-single-process"
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("target" in p for p in problems)


def test_wrong_manifest_version_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["manifest_version"] = MANIFEST_VERSION + 1
    assert not qualifier(report).verdict()[0]


# ── stage evidence rules ──────────────────────────────────────────────

def test_bare_shell_stage_without_evidence_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["stages"]["migrations"] = {"kind": "shell", "ok": True, "exit": 0}
    ok, problems = qualifier(report).verdict()
    assert not ok
    assert any("migrations" in p for p in problems)


def test_pytest_stage_with_zero_executed_fails(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["stages"]["unit_contract"] = write_result(
        "unit_contract", ev, counts={"passed": 0}, kind="pytest")
    assert not qualifier(report).verdict()[0]


def test_stage_result_file_hash_recorded(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    report["stages"]["unit_contract"].pop("result_sha256")
    assert not qualifier(report).verdict()[0]


def test_retry_after_failed_first_attempt_does_not_flip_stage(tmp_path):
    ev = make_evidence_dir(tmp_path)
    report = base_report(ev)
    failed = write_result("security", ev, counts={"passed": 3, "failed": 1},
                          kind="pytest")
    failed["ok"] = False
    report["stages"]["security"] = failed
    report["retries"] = {"security-fix": {
        "stage": "security", "label": "security-fix",
        "results": {"security": write_result(
            "security", ev, counts={"passed": 4}, kind="pytest")}}}
    ok, problems = qualifier(report).verdict()
    assert not ok, "a passing retry never repairs a failed first attempt"


# ── the genuine positive ──────────────────────────────────────────────

def test_genuine_packet_passes_verdict(tmp_path):
    ev = make_evidence_dir(tmp_path)
    ok, problems = qualifier(base_report(ev)).verdict()
    assert ok, problems
