"""Phase 31 §31.2 — journey evidence joins real product observations.

Named regressions (phase-31.md §31.2 negative checks + the November 13
report's first build priority):
  - a passing stage / workflow status / composed run ID cannot establish
    a product postcondition (passing unrelated case leaves journey open)
  - a generated run ID with no product record fails specifically
  - wrong-journey receipts, previous-attempt receipts, foreign
    candidates/targets, altered bytes, duplicate JSON keys
  - skipped mandatory subchecks, screenshots without assertions,
    passing receipts without cleanup
  - a product that returns COMPLETED with a deliberately wrong artifact
    is a verification failure, not a completion
  - an interrupted attempt retains its observations up to interruption
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from backend.tests._phase31_evidence import (
    CAND, DIGEST, OPERATOR_ATTEMPT, TARGET,
    scenario_record, scenario_section, write_scenario_receipt,
)
from scripts.qualify_release import (
    Qualifier, MANIFEST_VERSION, REQUIRED_JOURNEYS, REQUIRED_SCENARIOS,
    journey_record,
)
from scripts.scenario_receipts import (
    ReceiptError, load_receipt, validate_receipt,
)

CAND_B = "b" * 64


@pytest.fixture(autouse=True)
def evidence_root(monkeypatch, tmp_path):
    from scripts import qualify_release
    monkeypatch.setattr(qualify_release, "ATTEMPTS", tmp_path)


def minimal_stages(ev_dir: Path) -> dict:
    """The 18 mandatory stages with genuine retained result bytes."""
    stages = {}
    for name in ("migrations", "product_e2e_typecheck", "product_e2e_lint",
                 "product_e2e_build", "performance", "restore",
                 "target_deployment", "pilot", "live_inference_hosted"):
        path = ev_dir / f"{name}.json"
        body = {"ok": True, "exit": 0}
        if name == "migrations":
            body = {"db_profile": "sqlite-fresh", "migrations_ok": True,
                    "tables": 42, "ok": True, "exit": 0}
        if name == "target_deployment":
            body = {"ok": True, "exit": 0,
                    "image_digests": {k: "sha256://" + "d" * 60
                                      for k in ("api", "worker", "frontend")}}
        path.write_text(json.dumps(body))
        stages[name] = {"kind": "shell", "ok": True, "exit": 0,
                        "result_file": str(path),
                        "result_sha256": hashlib.sha256(
                            path.read_bytes()).hexdigest()}
    return stages


def passing_journey(ev_dir: Path, jid: str, run_id: str | None = None,
                    *, ok: bool = True) -> dict:
    ev_file = ev_dir / f"{jid}.json"
    ev_file.write_text(json.dumps(
        {"journey": jid, "run_id": run_id or f"run-{jid}",
         "observed": ok}))
    rec = journey_record(
        jid, candidate_sha256=CAND, target_id=TARGET,
        production_action=f"executed {jid} against the live stack",
        run_id=run_id or f"run-{jid}",
        expected_postcondition="independently verified product state",
        observed_result="observed independently",
        evidence=[{"result_file": str(ev_file),
                   "result_sha256": hashlib.sha256(
                       ev_file.read_bytes()).hexdigest()}])
    rec["target_image_digests_sha256"] = DIGEST
    if not ok:
        rec["ok"] = False
    return rec


def base_report(ev_dir: Path, *, scenarios: dict | None = None,
                journeys: dict | None = None) -> dict:
    return {
        "scope": "full", "manifest_version": MANIFEST_VERSION,
        "stages": minimal_stages(ev_dir),
        "journeys": journeys if journeys is not None else {
            jid: passing_journey(ev_dir, jid) for jid in REQUIRED_JOURNEYS},
        "scenarios": (scenarios if scenarios is not None
                      else scenario_section(ev_dir)),
        "candidate_identity": {"content_sha256": CAND},
        "target_identity": {"profile": TARGET,
                            "image_digests_sha256": DIGEST},
        "source_changed": False,
    }


def qualifier(report, attempt_dir) -> Qualifier:
    q = Qualifier.__new__(Qualifier)
    q.strict = False
    q.attempt_id = "p31-test-attempt"
    q.attempt_dir = attempt_dir
    q.report = report
    return q


# ── run provenance ────────────────────────────────────────────────────

def test_composed_run_id_cannot_back_a_passing_journey(tmp_path):
    """The producer's composed reference documents an open journey — a
    record claiming ok with `J01:unobserved` is refused."""
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    journeys["J01"]["run_id"] = "J01:unobserved"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J01" in p and "not a product record" in p for p in problems)


def test_stage_sha_run_reference_cannot_pass(tmp_path):
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    journeys["J09"]["run_id"] = "J09:stage:0123abcd"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J09" in p and "not a product record" in p for p in problems)


def test_generated_run_id_without_product_record_fails(tmp_path):
    """A run ID no retained receipt names cannot establish the
    postcondition — even when the journey record is otherwise complete."""
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    journeys["J05"]["run_id"] = "run-fabricated-after-the-fact"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J05" in p and "no retained product record" in p
               for p in problems)


def test_observation_must_name_the_run_it_claims(tmp_path):
    """An observation file that does not contain the journey's run ID is
    a broken binding, not provenance."""
    obs = tmp_path / "j16-observation.json"
    obs.write_text(json.dumps({"journey_id": "J16",
                               "run_id": "run-some-other-journey"}))
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    journeys["J16"]["observation"] = {
        "result_file": str(obs),
        "result_sha256": hashlib.sha256(obs.read_bytes()).hexdigest()}
    journeys["J16"]["run_id"] = "run-j16-product"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J16" in p and "does not name run" in p for p in problems)


def test_observation_bound_to_a_different_candidate_fails(tmp_path):
    obs = tmp_path / "j03-observation.json"
    obs.write_text(json.dumps({"journey_id": "J03",
                               "run_id": "run-j03-observed",
                               "candidate_sha256": CAND_B}))
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    journeys["J03"]["observation"] = {
        "result_file": str(obs),
        "result_sha256": hashlib.sha256(obs.read_bytes()).hexdigest()}
    journeys["J03"]["run_id"] = "run-j03-observed"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J03" in p and "not bound to this candidate" in p
               for p in problems)


# ── scenario receipt contract ─────────────────────────────────────────

def test_wrong_journey_receipt_fails(tmp_path):
    """A receipt for one journey attached to another is wrong-journey
    evidence."""
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    receipt_path = write_scenario_receipt(
        tmp_path, "EXISTING-TOOL", "run-existing",
        journey_id="J01")
    rec = journeys["J07"]
    rec["run_id"] = "run-existing"
    rec["scenario_receipts"] = [{
        "scenario_id": "EXISTING-TOOL", "scenario_version": 1,
        "result_file": str(receipt_path),
        "result_sha256": hashlib.sha256(
            receipt_path.read_bytes()).hexdigest()}]
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J07" in p and "wrong-journey evidence" in p
               for p in problems), problems[:8]


def test_previous_operator_attempt_receipt_fails(tmp_path):
    """A receipt retained by a PREVIOUS operator attempt is not this
    verdict's evidence."""
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    receipt_path = write_scenario_receipt(
        tmp_path, "TAKEOVER-RETURN", "run-takeover",
        journey_id="J07", attempt="p31-earlier-attempt")
    rec = journeys["J07"]
    rec["run_id"] = "run-takeover"
    rec["attempt_id"] = OPERATOR_ATTEMPT
    rec["scenario_receipts"] = [{
        "scenario_id": "TAKEOVER-RETURN", "scenario_version": 1,
        "result_file": str(receipt_path),
        "result_sha256": hashlib.sha256(
            receipt_path.read_bytes()).hexdigest()}]
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("p31-earlier-attempt" in p for p in problems), problems[:8]


def test_altered_receipt_bytes_fail(tmp_path):
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    receipt_path = write_scenario_receipt(
        tmp_path, "WORKER-REPLAY", "run-replay", journey_id="J09")
    recorded_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(
        {"schema": "phase31-scenario-receipt/1", "scenario_id":
         "WORKER-REPLAY", "status": "passed", "ok": True}))
    rec = journeys["J09"]
    rec["run_id"] = "run-replay"
    rec["scenario_receipts"] = [{
        "scenario_id": "WORKER-REPLAY", "scenario_version": 1,
        "result_file": str(receipt_path),
        "result_sha256": recorded_sha}]
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("bytes changed" in p for p in problems), problems[:8]


def test_duplicate_json_key_in_receipt_fails(tmp_path):
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    receipt_path = write_scenario_receipt(
        tmp_path, "WORKER-REPLAY", "run-replay", journey_id="J09")
    raw = receipt_path.read_text()
    receipt_path.write_text(
        raw[:-1] + ', "status": "passed"}')
    with pytest.raises(ReceiptError):
        load_receipt(receipt_path)
    rec = journeys["J09"]
    rec["run_id"] = "run-replay"
    rec["scenario_receipts"] = [{
        "scenario_id": "WORKER-REPLAY", "scenario_version": 1,
        "result_file": str(receipt_path),
        "result_sha256": hashlib.sha256(
            receipt_path.read_bytes()).hexdigest()}]
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("duplicate JSON key" in p for p in problems), problems[:8]


def test_skipped_mandatory_subcheck_fails(tmp_path):
    """A receipt that claims `passed` while skipping a mandatory
    assertion declared by the catalog fails."""
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "RESUME-PDF", "run-resume", journey_id="J16"))
    mandatory = catalog_scenario_assertions("RESUME-PDF")
    receipt["assertions"] = [a for a in receipt["assertions"]
                             if a["id"] != mandatory[0]]
    receipt_path = tmp_path / "scenarios" / "RESUME-PDF-partial.json"
    receipt_path.write_text(json.dumps(receipt))
    problems = validate_receipt(receipt, expected_scenario_id="RESUME-PDF",
                                expected_journey_id="J16")
    assert any("skipped mandatory subchecks" in p for p in problems), problems


def catalog_scenario_assertions(scenario_id: str) -> list[str]:
    from scripts.scenario_receipts import catalog_scenario
    return catalog_scenario(scenario_id)["mandatory_assertions"]


def test_failed_assertion_with_passed_status_fails(tmp_path):
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "RESUME-PDF", "run-resume", journey_id="J16"))
    receipt["assertions"][0]["ok"] = False
    receipt["assertions"][0]["observed"] = "a different revision"
    problems = validate_receipt(receipt, expected_scenario_id="RESUME-PDF")
    assert any("failed assertions" in p for p in problems), problems


def test_screenshot_without_assertion_fails(tmp_path):
    shot = tmp_path / "shots" / "hover-card.png"
    shot.parent.mkdir()
    shot.write_bytes(b"png-bytes")
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "LINK-PREVIEW", "run-link", journey_id=None))
    receipt["evidence"].append({
        "kind": "screenshot", "result_file": str(shot),
        "result_sha256": hashlib.sha256(shot.read_bytes()).hexdigest(),
        "assertion_id": "A99-never-recorded"})
    problems = validate_receipt(receipt, expected_scenario_id="LINK-PREVIEW")
    assert any("no corresponding assertion" in p for p in problems), problems


def test_screenshot_bytes_are_hash_verified(tmp_path):
    shot = tmp_path / "shots" / "docked.png"
    shot.parent.mkdir()
    shot.write_bytes(b"png-bytes")
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "LINK-PREVIEW", "run-link", journey_id=None))
    receipt["evidence"].append({
        "kind": "screenshot", "result_file": str(shot),
        "result_sha256": "0" * 64, "assertion_id":
        receipt["assertions"][0]["id"]})
    from scripts.scenario_receipts import evidence_hash_problems
    from scripts.qualify_release import verify_result_file
    problems = evidence_hash_problems(receipt, tmp_path, verify_result_file)
    assert any("screenshot bytes changed" in p for p in problems), problems


def test_passing_receipt_without_cleanup_fails(tmp_path):
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "MEMORY-INVALIDATION", "run-memory", journey_id="J15"))
    receipt["cleanup"] = {"ok": False, "detail": "replica still running"}
    problems = validate_receipt(receipt, expected_scenario_id=
                                "MEMORY-INVALIDATION")
    assert any("cleanup did not succeed" in p for p in problems), problems


def test_passing_receipt_without_real_run_fails(tmp_path):
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "MODEL-EDIT-EXPORT", "run-model", journey_id="J24"))
    receipt["relations"] = {"run_ids": []}
    receipt["observation"] = {"kind": "independent", "detail": "none"}
    problems = validate_receipt(receipt, expected_scenario_id=
                                "MODEL-EDIT-EXPORT")
    assert any("no real product run record" in p for p in problems), problems


def test_failed_assertion_receipt_observation_is_retained(tmp_path):
    """An interrupted attempt retains its observations; the journey
    stays open with the recorded failure (never silently dropped)."""
    journeys = {jid: passing_journey(tmp_path, jid)
                for jid in REQUIRED_JOURNEYS}
    write_scenario_receipt(
        tmp_path, "BOUNDED-REPAIR", "", journey_id="J11",
        status="cancelled", assertions_override=[
            {"id": "A1-transient-fault-recovers-in-limits",
             "expected": "recovery", "observed": "outage persisted",
             "ok": False}])
    rec = journeys["J11"]
    rec["ok"] = False
    rec["status"] = "cancelled"
    ok, problems = qualifier(base_report(tmp_path, journeys=journeys),
                             tmp_path).verdict()
    assert not ok
    assert any("J11" in p and "recorded failure" in p for p in problems)


# ── the mandatory journey-independent scenarios ───────────────────────

def test_missing_mandatory_scenario_evidence_fails(tmp_path):
    ok, problems = qualifier(base_report(tmp_path, scenarios={}),
                             tmp_path).verdict()
    assert not ok
    for sid in REQUIRED_SCENARIOS:
        assert any(f"scenario {sid}" in p and "missing" in p
                   for p in problems), (sid, problems[:8])


def test_scenario_bound_to_foreign_candidate_fails(tmp_path):
    scenarios = scenario_section(tmp_path, candidate=CAND_B)
    ok, problems = qualifier(base_report(tmp_path, scenarios=scenarios),
                             tmp_path).verdict()
    assert not ok
    assert any("different candidate" in p and "LINK-PREVIEW" in p
               for p in problems), problems[:8]


def test_scenario_passing_without_receipt_fails(tmp_path):
    scenarios = {sid: scenario_record(tmp_path, sid, f"run-{sid.lower()}")
                 for sid in REQUIRED_SCENARIOS}
    del scenarios["ORDINARY-CHAT"]["scenario_receipts"]
    ok, problems = qualifier(base_report(tmp_path, scenarios=scenarios),
                             tmp_path).verdict()
    assert not ok
    assert any("ORDINARY-CHAT" in p and "without a scenario receipt" in p
               for p in problems), problems[:8]


def test_unknown_scenario_id_fails(tmp_path):
    scenarios = scenario_section(tmp_path)
    scenarios["MADE-UP-SCENARIO"] = scenarios["ORDINARY-CHAT"]
    ok, problems = qualifier(base_report(tmp_path, scenarios=scenarios),
                             tmp_path).verdict()
    assert not ok
    assert any("not a mandatory scenario id" in p for p in problems)


# ── the §31.2 doc regression: COMPLETED with a wrong artifact ─────────

def test_completed_product_with_wrong_artifact_fails(tmp_path):
    """The product returns COMPLETED but the downloaded artifact is a
    different revision: a receipt claiming `passed` with a failed
    assertion is rejected — the COMPLETED status cannot substitute for
    the mismatched bytes."""
    receipt = load_receipt(write_scenario_receipt(
        tmp_path, "RESUME-PDF", "run-resume-product", journey_id="J16",
        status="passed"))
    receipt["assertions"][1]["ok"] = False
    receipt["assertions"][1]["observed"] = \
        "downloaded bytes hash to a different revision"
    problems = validate_receipt(receipt, expected_scenario_id="RESUME-PDF",
                                expected_journey_id="J16")
    assert any("failed assertions" in p for p in problems), problems
    assert any("A2-publish-records-revision-and-sha256" in p
               for p in problems), problems


# ── the producer consumes receipts / keeps journeys open ──────────────

def _attempt_dir(tmp_path: Path) -> Path:
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    report = {
        "candidate_identity": {"content_sha256": CAND},
        "target_identity": {"profile": TARGET,
                            "image_digests_sha256": DIGEST},
        "stages": {},
    }
    (attempt / "report.json").write_text(json.dumps(report))
    return attempt


def _run_producer(attempt: Path) -> dict:
    from scripts.generate_journey_evidence import main as produce
    argv = sys.argv
    sys.argv = ["generate_journey_evidence.py", str(attempt)]
    try:
        assert produce() == 0
    finally:
        sys.argv = argv
    return json.loads((attempt / "journey-evidence.json").read_text())


def test_passing_unrelated_stage_leaves_journey_open(tmp_path):
    """THE November 13 defect: a green covering stage without a product
    observation leaves the journey OPEN."""
    attempt = _attempt_dir(tmp_path)
    stage = tmp_path / "flagship.xml"
    stage.write_text("<testsuites><testsuite name='flagship'/></testsuites>")
    attempt_report = json.loads((attempt / "report.json").read_text())
    attempt_report["stages"]["flagship"] = {
        "ok": True, "result_file": str(stage),
        "result_sha256": hashlib.sha256(stage.read_bytes()).hexdigest()}
    (attempt / "report.json").write_text(json.dumps(attempt_report))
    evidence = _run_producer(attempt)
    j01 = evidence["J01"]
    assert j01["ok"] is False
    assert j01["status"] == "unobserved"
    assert j01["run_id"] == "J01:unobserved"
    assert "not a product postcondition" in j01["observed_result"]
    # supporting stage coverage is still recorded
    assert any(s["stage"] == "flagship" for s in j01["evidence"])


def test_producer_consumes_scenario_receipts(tmp_path):
    """A passing scenario receipt closes its journey with the REAL run
    ID, the operator attempt, and the observation retained."""
    attempt = _attempt_dir(tmp_path)
    write_scenario_receipt(attempt, "EXISTING-TOOL", "run-real-42",
                           journey_id="J01", attempt=OPERATOR_ATTEMPT)
    evidence = _run_producer(attempt)
    j01 = evidence["J01"]
    assert j01["ok"] is True
    assert j01["status"] == "passed"
    assert j01["run_id"] == "run-real-42"
    assert j01["attempt_id"] == OPERATOR_ATTEMPT
    assert j01["scenario_receipts"][0]["scenario_id"] == "EXISTING-TOOL"
    # Supporting stage entries appear only when the covering stages
    # retained result bytes — an entry without bytes is not evidence.
    assert all(r.get("result_file") for r in j01["evidence"])


def test_producer_requires_independent_observation(tmp_path):
    """Without any receipt or observation the producer keeps the journey
    open — even when the covering stages passed."""
    attempt = _attempt_dir(tmp_path)
    attempt_report = json.loads((attempt / "report.json").read_text())
    for sid in ("flagship", "fault_injection"):
        attempt_report["stages"][sid] = {"ok": True}
    (attempt / "report.json").write_text(json.dumps(attempt_report))
    evidence = _run_producer(attempt)
    assert all(j["ok"] is False for j in evidence.values() if j != evidence["scenarios"])
    assert evidence["scenarios"]["LINK-PREVIEW"]["ok"] is False


def test_producer_rejects_wrong_journey_receipt(tmp_path):
    """A receipt whose journey binding mismatches cannot close the
    journey — the producer reports the specific problem."""
    attempt = _attempt_dir(tmp_path)
    write_scenario_receipt(attempt, "WORKER-REPLAY", "run-replay",
                           journey_id="J07")
    evidence = _run_producer(attempt)
    j09 = evidence["J09"]
    assert j09["ok"] is False
    assert j09["status"] == "receipt_rejected"
    assert "wrong-journey evidence" in j09["observed_result"]


def test_producer_emits_scenario_verdicts(tmp_path):
    attempt = _attempt_dir(tmp_path)
    write_scenario_receipt(attempt, "LINK-PREVIEW", "run-link-preview",
                           journey_id=None)
    evidence = _run_producer(attempt)
    lp = evidence["scenarios"]["LINK-PREVIEW"]
    assert lp["ok"] is True
    assert lp["run_id"] == "run-link-preview"
    assert evidence["scenarios"]["ORDINARY-CHAT"]["ok"] is False
