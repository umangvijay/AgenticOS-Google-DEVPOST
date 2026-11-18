"""Phase 29 — qualifier gate extensions (§29.2/§29.3).

Named regressions beyond the Phase 27/28 gates: a frontend report
recorded under the wrong stage identity, duplicate JSON keys in a
frontend runner report, journey evidence retained under a SIBLING
attempt directory, the three previously-untested negative branches
(performance ledger contradiction, restore count mismatch, target
digest cross-check), and verify_candidate's manifest self-consistency
plus archive tamper refusals.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from backend.tests.test_phase27_stage_schemas import _workload_body
import pytest

from scripts.qualify_release import (
    Qualifier, REQUIRED_STAGES, REQUIRED_JOURNEYS, MANIFEST_VERSION,
    journey_record, parse_retained_counts, frontend_counts,
)
from scripts import qualify_release as qr

CAND = "a" * 64
TARGET = "local-durable-postgres"


@pytest.fixture(autouse=True)
def evidence_root(monkeypatch, tmp_path):
    monkeypatch.setattr(qr, "ATTEMPTS", tmp_path)


def _shell(ev_dir, name, body, extra=None):
    path = ev_dir / f"{name}.json"
    path.write_text(json.dumps(body))
    record = {"kind": "shell", "ok": True, "exit": 0,
              "result_file": str(path),
              "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    record.update(extra or {})
    return record


def _junit(ev_dir, stage, passed=3):
    cases = "".join(f'<testcase name="t{i}"/>' for i in range(passed))
    path = ev_dir / f"{stage}.xml"
    path.write_bytes(
        f'<testsuites><testsuite name="{stage}">{cases}</testsuite>'
        f"</testsuites>".encode())
    return {"kind": "pytest", "ok": True, "exit": 0,
            "counts": {"passed": passed},
            "result_file": str(path),
            "result_sha256": hashlib.sha256(
                path.read_bytes()).hexdigest()}


def _frontend_envelope(stage, runner, report):
    return {"stage": stage, "runner": runner,
            "command": ["npm", "test"], "exit": 0, "report": report}


def base_stages(ev_dir, *, vitest_report=None, browser_report=None):
    stages = {}
    for name in REQUIRED_STAGES:
        if name in ("unit_contract", "fault_injection", "security",
                    "infra_integration", "live_inference_local", "flagship",
                    "rl_training"):
            stages[name] = _junit(ev_dir, name)
        elif name == "product_e2e_vitest":
            stages[name] = _shell(ev_dir, name, vitest_report or
                                  _frontend_envelope(name, "vitest", {
                                      "numPassedTests": 90,
                                      "numFailedTests": 0,
                                      "numPendingTests": 0,
                                      "numRuntimeErrorTestSuites": 0}),
                                  extra={"counts": {"passed": 90}})
        elif name == "product_e2e_browser":
            stages[name] = _shell(ev_dir, name, browser_report or
                                  _frontend_envelope(name, "playwright", {
                                      "stats": {"expected": 28,
                                                "unexpected": 0,
                                                "skipped": 0, "flaky": 0},
                                      "errors": []}),
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
            digests = {k: "sha256:" + "d" * 64
                       for k in ("api", "worker", "frontend")}
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0, "image_digests": digests,
                "workflow": {"submitted": True, "completed": True,
                             "run_id": "run-x",
                             "final_status": "COMPLETED"}},
                extra={"image_digests": digests})
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
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0, "providers": providers})
        elif name == "migrations":
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0, "db_profile": "sqlite-fresh",
                "migrations_ok": True, "tables": 42})
        elif name == "pilot":
            sessions = []
            for i, participant in enumerate(("p1", "p2", "p3")):
                trace = ev_dir / f"pilot-{i}.json"
                trace.write_text(json.dumps(
                    {"session_id": f"s-{i}", "run_id": f"pr-{i}",
                     "verified_outcome": True}))
                sessions.append({
                    "session_id": f"s-{i}",
                    "participant_ref": participant,
                    "consent_ref": f"c-{i}", "run_id": f"pr-{i}",
                    "verified_outcome": True,
                    "result_file": str(trace),
                    "result_sha256": hashlib.sha256(
                        trace.read_bytes()).hexdigest()})
            stages[name] = _shell(ev_dir, name, {
                "ok": True, "exit": 0, "sessions": sessions})
        else:  # pragma: no cover
            raise AssertionError(f"unhandled stage {name}")
    return stages


def base_report(ev_dir, stages=None):
    from backend.tests._phase31_evidence import (
        journey_evidence_file, scenario_section,
    )
    journeys = {}
    for jid in REQUIRED_JOURNEYS:
        ev_file = journey_evidence_file(ev_dir, jid)
        evidence = [{"result_file": str(ev_file),
                     "result_sha256": hashlib.sha256(
                         ev_file.read_bytes()).hexdigest()}]
        # J14 is bound to rl_training: its evidence must carry the bound
        # stage's own retained receipt.
        if jid == "J14":
            rl = (stages or base_stages(ev_dir))["rl_training"]
            evidence.append({"result_file": rl["result_file"],
                             "result_sha256": rl["result_sha256"]})
        rec = journey_record(
            jid, candidate_sha256=CAND, target_id=TARGET,
            production_action=f"executed {jid} against the live stack",
            run_id=f"run-{jid}", expected_postcondition="COMPLETED",
            observed_result="COMPLETED with verified output",
            evidence=evidence)
        rec["target_image_digests_sha256"] = "d" * 64
        journeys[jid] = rec
    return {"scope": "full", "manifest_version": MANIFEST_VERSION,
            "stages": stages or base_stages(ev_dir), "journeys": journeys,
            "scenarios": scenario_section(ev_dir),
            "candidate_identity": {"content_sha256": CAND},
            "target_identity": {"profile": TARGET,
                                "image_digests_sha256": "d" * 64},
            "source_changed": False}


def qualifier(report, attempt_dir):
    q = Qualifier.__new__(Qualifier)
    q.strict = False
    q.attempt_id = "this-attempt"
    q.attempt_dir = attempt_dir
    q.report = report
    return q


# ── §29.2 frontend identity envelope ─────────────────────────────────

def test_frontend_report_under_wrong_stage_identity_fails(tmp_path):
    """A vitest report cannot satisfy the browser stage — the envelope's
    stage/runner identity is checked, like the JUnit suite name."""
    stages = base_stages(tmp_path, browser_report=_frontend_envelope(
        "product_e2e_vitest", "vitest", {
            "stats": {"expected": 28, "unexpected": 0, "skipped": 0,
                      "flaky": 0},
            "errors": []}))
    problems = parse_retained_counts(
        "product_e2e_browser", stages["product_e2e_browser"], tmp_path)
    assert any("identity envelope" in p for p in problems), problems


def test_frontend_report_with_duplicate_keys_fails(tmp_path):
    """Duplicate JSON keys in a retained frontend report are refused —
    last-wins parsing cannot pick a passing count."""
    path = tmp_path / "product_e2e_vitest.json"
    path.write_text(
        '{"stage": "product_e2e_vitest", "runner": "vitest", '
        '"command": ["npm"], "exit": 0, "report": {"numPassedTests": 90}, '
        '"report": {"numPassedTests": 0}}')
    record = {"kind": "shell", "ok": True, "exit": 0,
              "result_file": str(path),
              "result_sha256": hashlib.sha256(
                  path.read_bytes()).hexdigest(),
              "counts": {"passed": 90}}
    problems = parse_retained_counts(
        "product_e2e_vitest", record, tmp_path)
    assert any("duplicate JSON key" in p for p in problems), problems


def test_genuine_frontend_envelope_passes(tmp_path):
    report = _frontend_envelope("product_e2e_vitest", "vitest", {
        "numPassedTests": 90, "numFailedTests": 0, "numPendingTests": 0,
        "numRuntimeErrorTestSuites": 0})
    path = tmp_path / "vitest.json"
    path.write_text(json.dumps(report))
    counts = frontend_counts(path, "vitest", expected_stage="product_e2e_vitest")
    assert counts == {"passed": 90, "failed": 0, "skipped": 0, "errors": 0}


def test_complete_packet_with_envelopes_passes(tmp_path):
    ok, problems = qualifier(base_report(tmp_path),
                             tmp_path).verdict()
    assert ok, problems


# ── §29.2 per-attempt evidence scoping ───────────────────────────────

def test_stage_evidence_from_a_sibling_attempt_fails(tmp_path):
    """Evidence retained under another attempt's directory is not this
    attempt's evidence — even with identical candidate identity."""
    sibling = tmp_path / "sibling-attempt"
    sibling.mkdir()
    stages = base_stages(sibling)
    this_attempt = tmp_path / "this-attempt"
    this_attempt.mkdir()
    ok, problems = qualifier(base_report(sibling, stages),
                             this_attempt).verdict()
    assert not ok
    assert any("outside the approved evidence location" in p
               for p in problems), problems[:6]


def test_journey_evidence_from_a_sibling_attempt_fails(tmp_path):
    """A journey whose evidence entries live in another attempt's
    directory cannot satisfy this attempt's verdict."""
    sibling = tmp_path / "sibling-attempt"
    sibling.mkdir()
    ev_file = sibling / "J01.json"
    ev_file.write_text(json.dumps({"journey": "J01", "observed": True}))
    journeys = {}
    for jid in REQUIRED_JOURNEYS:
        rec = journey_record(
            jid, candidate_sha256=CAND, target_id=TARGET,
            production_action=f"executed {jid}", run_id=f"run-{jid}",
            expected_postcondition="COMPLETED",
            observed_result="COMPLETED",
            evidence=[{"result_file": str(ev_file),
                       "result_sha256": hashlib.sha256(
                           ev_file.read_bytes()).hexdigest()}])
        rec["target_image_digests_sha256"] = "d" * 64
        journeys[jid] = rec
    this_attempt = tmp_path / "this-attempt"
    this_attempt.mkdir()
    report = base_report(this_attempt)
    report["journeys"] = journeys
    ok, problems = qualifier(report, this_attempt).verdict()
    assert not ok
    assert any("outside the approved evidence location" in p
               for p in problems), problems[:6]


# ── previously-untested negative branches ────────────────────────────

def test_performance_ledger_contradiction_fails(tmp_path):
    body = {"workload": "admission", "exit": 0,
            "rate_limit": {"ok": True, "allowance": 12,
                           "accepted_total": 12, "ledger_count": 7},
            "capacity": {"ok": True, "skipped": False}, "ok": True}
    stages = base_stages(tmp_path)
    stages["performance"] = _shell(tmp_path, "performance", body)
    problems = parse_retained_counts("performance",
                                     stages["performance"], tmp_path)
    assert any("ledger" in p for p in problems), problems


def test_performance_allowance_contradiction_fails(tmp_path):
    body = {"workload": "admission", "exit": 0,
            "rate_limit": {"ok": True, "allowance": 12,
                           "accepted_total": 9},
            "capacity": {"ok": True, "skipped": False}, "ok": True}
    stages = base_stages(tmp_path)
    stages["performance"] = _shell(tmp_path, "performance", body)
    problems = parse_retained_counts("performance",
                                     stages["performance"], tmp_path)
    assert any("allowance" in p for p in problems), problems


def test_restore_count_mismatch_fails(tmp_path):
    body = {"ok": True, "exit": 0,
            "subchecks": {k: "ok" for k in (
                "dump", "restore", "integrity", "cleanup",
                "independent_app_trace")},
            "source_counts": {"workflows": 268, "events": 1051},
            "restored_counts": {"workflows": 267, "events": 1051}}
    stages = base_stages(tmp_path)
    stages["restore"] = _shell(tmp_path, "restore", body)
    problems = parse_retained_counts("restore", stages["restore"], tmp_path)
    assert any("counts do not match" in p for p in problems), problems


def test_target_digest_cross_check_fails(tmp_path):
    digests = {k: "sha256:" + "d" * 64
               for k in ("api", "worker", "frontend")}
    other = {k: "sha256:" + "e" * 64
             for k in ("api", "worker", "frontend")}
    stages = base_stages(tmp_path)
    stages["target_deployment"] = _shell(
        tmp_path, "target_deployment", {
            "ok": True, "exit": 0, "image_digests": digests,
            "workflow": {"submitted": True, "completed": True,
                         "run_id": "run-x", "final_status": "COMPLETED"}},
        extra={"image_digests": other})
    report = base_report(tmp_path, stages)
    ok, problems = qualifier(report, tmp_path).verdict()
    assert not ok
    assert any("image identities contradict" in p for p in problems), \
        problems[:6]


# ── §29.3 verify_candidate self-consistency and tamper refusals ──────

def _make_candidate(tmp_path, monkeypatch):
    """Construct a tiny candidate exactly as the freezer would — real
    shared fingerprint, real self-contained archive, real manifest —
    so verify_candidate's contract is exercised end to end."""
    import datetime as dt
    from scripts.source_identity import fingerprint_from_hashes
    (tmp_path / "app.py").write_text("VALUE = 1\n")
    file_hashes = {"app.py": {"sha256": hashlib.sha256(
        (tmp_path / "app.py").read_bytes()).hexdigest(),
        "mode": "0o644"}}
    content_sha256 = fingerprint_from_hashes(file_hashes)
    cand_root = tmp_path / "docs" / "evidence" / "candidates"
    cand_root.mkdir(parents=True)
    candidate_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" \
        + content_sha256[:12]
    cand_dir = cand_root / candidate_id
    cand_dir.mkdir()
    (cand_dir / "file-hashes.json").write_text(
        json.dumps(file_hashes, indent=2, sort_keys=True))
    import tarfile
    archive_path = cand_dir / "source-archive.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(tmp_path / "app.py", arcname="app.py")
    manifest = {
        "candidate_id": candidate_id,
        "source_archive": {"path": "source-archive.tar.gz",
                           "sha256": hashlib.sha256(
                               archive_path.read_bytes()).hexdigest()},
        "manifest_version": 2,
        "source": {"content_sha256": content_sha256},
        "verification": {"expected_content_sha256": content_sha256},
    }
    (cand_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return tmp_path, candidate_id


def test_verify_candidate_rejects_edited_manifest_fingerprint(
        tmp_path, monkeypatch):
    """Editing file-hashes.json and expected_content_sha256 TOGETHER must
    not produce a RECONSTRUCTIBLE verdict — the manifest must reproduce
    the expected fingerprint from its own frozen hashes."""
    tmp_path, candidate_id = _make_candidate(tmp_path, monkeypatch)
    cand_dir = tmp_path / "docs" / "evidence" / "candidates" / candidate_id
    hashes = json.loads((cand_dir / "file-hashes.json").read_text())
    # Tamper with one file's recorded hash AND the expected fingerprint
    # so they "agree" with each other but not with reality.
    from scripts.source_identity import fingerprint_from_hashes
    tampered = dict(hashes)
    tampered["app.py"] = {**tampered["app.py"],
                          "sha256": "f" * 64}
    expected = fingerprint_from_hashes(tampered)
    manifest = json.loads((cand_dir / "manifest.json").read_text())
    manifest["verification"]["expected_content_sha256"] = expected
    (cand_dir / "file-hashes.json").write_text(json.dumps(tampered))
    (cand_dir / "manifest.json").write_text(json.dumps(manifest))
    from scripts import verify_candidate
    # Only CANDIDATES moves — ROOT must stay the real repo so the
    # shared source_identity module imports normally.
    monkeypatch.setattr(verify_candidate, "CANDIDATES",
                        tmp_path / "docs" / "evidence" / "candidates")
    rc = verify_candidate.main.__wrapped__(candidate_id) if False else None
    # Run main with argv patched.
    import sys
    monkeypatch.setattr(sys, "argv", ["verify_candidate.py", candidate_id])
    rc = verify_candidate.main()
    assert rc == 1
    # And the same run through the real pipeline names the problem.
    capsys_probe = None  # output asserted via return code


def test_verify_candidate_rejects_corrupted_archive(tmp_path, monkeypatch):
    tmp_path, candidate_id = _make_candidate(tmp_path, monkeypatch)
    cand_dir = tmp_path / "docs" / "evidence" / "candidates" / candidate_id
    archive = cand_dir / "source-archive.tar.gz"
    data = archive.read_bytes()
    archive.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    from scripts import verify_candidate
    # Only CANDIDATES moves — ROOT must stay the real repo so the
    # shared source_identity module imports normally.
    monkeypatch.setattr(verify_candidate, "CANDIDATES",
                        tmp_path / "docs" / "evidence" / "candidates")
    import sys
    monkeypatch.setattr(sys, "argv", ["verify_candidate.py", candidate_id])
    rc = verify_candidate.main()
    assert rc == 1


def test_verify_candidate_rejects_tampered_member(tmp_path, monkeypatch):
    """A member whose bytes differ from the frozen hash — repacked into a
    fresh archive with a matching archive sha — is NOT RECONSTRUCTIBLE."""
    tmp_path, candidate_id = _make_candidate(tmp_path, monkeypatch)
    cand_dir = tmp_path / "docs" / "evidence" / "candidates" / candidate_id
    manifest = json.loads((cand_dir / "manifest.json").read_text())
    archive_path = cand_dir / manifest["source_archive"]["path"]
    # Repack with modified content.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with tarfile.open(archive_path) as tar:
            tar.extractall(d, filter="data")
        target = Path(d) / "app.py"
        target.write_text("VALUE = 999\n")
        repacked = cand_dir / "repacked.tar.gz"
        with tarfile.open(repacked, "w:gz") as tar:
            for name in sorted(p.name for p in Path(d).rglob("*")
                               if p.is_file()):
                tar.add(Path(d) / name, arcname=name)
        archive_path.unlink()
        shutil.move(str(repacked), str(archive_path))
    manifest["source_archive"]["sha256"] = hashlib.sha256(
        archive_path.read_bytes()).hexdigest()
    (cand_dir / "manifest.json").write_text(json.dumps(manifest))
    from scripts import verify_candidate
    # Only CANDIDATES moves — ROOT must stay the real repo so the
    # shared source_identity module imports normally.
    monkeypatch.setattr(verify_candidate, "CANDIDATES",
                        tmp_path / "docs" / "evidence" / "candidates")
    import sys
    monkeypatch.setattr(sys, "argv", ["verify_candidate.py", candidate_id])
    rc = verify_candidate.main()
    assert rc == 1


def test_verify_candidate_accepts_an_unchanged_candidate(
        tmp_path, monkeypatch):
    tmp_path, candidate_id = _make_candidate(tmp_path, monkeypatch)
    from scripts import verify_candidate
    # Only CANDIDATES moves — ROOT must stay the real repo so the
    # shared source_identity module imports normally.
    monkeypatch.setattr(verify_candidate, "CANDIDATES",
                        tmp_path / "docs" / "evidence" / "candidates")
    import sys
    monkeypatch.setattr(sys, "argv", ["verify_candidate.py", candidate_id])
    rc = verify_candidate.main()
    assert rc == 0
