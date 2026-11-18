"""Contract fixtures for the release harness; these never count as live release receipts."""
import hashlib
import json

import pytest

from scripts import qualify_release as release

SOURCE = "a" * 64
IMAGES = "b" * 64
PROFILE = "test-only"


def write(path, body):
    raw = json.dumps(body).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(release, "ATTEMPTS", tmp_path / "attempts")
    monkeypatch.setattr(release, "git_identity", lambda: {"content_sha256": SOURCE})
    monkeypatch.setattr(release, "runtime_identity", lambda: {})
    monkeypatch.setattr(release, "readiness", lambda *args: (False, "unit-test"))
    monkeypatch.setenv("TARGET_PROFILE", PROFILE)
    q = release.Qualifier(strict=True)
    q.report.update(candidate_identity={"content_sha256": SOURCE},
                    target_identity={"profile": PROFILE, "image_digests_sha256": IMAGES})
    return q


def packet(tmp_path, stage):
    directory = tmp_path / "reviewed"
    directory.mkdir()
    identity = {"stage": stage, "candidate_sha256": SOURCE, "target_profile": PROFILE, "image_digests_sha256": IMAGES}
    body = {**identity, "ok": True, "exit": 0}
    if stage == "pilot":
        body["sessions"] = []
        for i in range(3):
            row = {"participant_ref": f"participant-{i}", "consent_ref": f"consent-{i}",
                   "run_id": f"run-{i}", "session_id": f"session-{i}", "verified_outcome": True}
            trace = {**identity, **row, "unassisted_tasks_1_to_3": [True, True, True], "takeover_attempted": True,
                     "takeover_succeeded": True, "stale_action_refused": True, "tenant_isolation_verified": True,
                     "failures_reconciled": True}
            name = f"session-{i}.json"
            body["sessions"].append({**row, "result_file": name, "result_sha256": write(directory / name, trace)})
    else:
        body["providers"] = {}
        for provider in ["azure", "bedrock", "vertex"]:
            row = {"provider_request_id": f"request-{provider}", "run_id": f"run-{provider}", "model": provider,
                   "checks": {k: True for k in ("workflow","tool_call","usage","cancellation","rotation",
                                                  "revocation","streaming","error")}}
            name = provider + ".json"
            body["providers"][provider] = {**row, "result_file": name,
                "result_sha256": write(directory / name, {**identity, **row, "provider": provider,
                    "provider_usage": {"input_tokens": 32, "output_tokens": 5}, "tool_call_id": "fixture-tool",
                    "tool_result_sha256": "c"*64, "cancelled_run_id": "fixture-cancelled", "cancelled_status": "CANCELLED",
                    "rotation_request_id": "fixture-rotation", "revocation_request_id": "fixture-revocation",
                    "error_request_id": "fixture-error", "stream_events": 2, "rotated_credential_succeeded": True,
                    "revoked_credential_denied": True})}
    return directory / "manifest.json", body


@pytest.mark.parametrize("stage,variable", [("pilot", "QUALIFY_PILOT_EVIDENCE"),
                                           ("live_inference_hosted", "QUALIFY_HOSTED_EVIDENCE")])
def test_external_stage_round_trip(tmp_path, runner, monkeypatch, stage, variable):
    path, body = packet(tmp_path, stage)
    monkeypatch.setenv(variable, str(path))
    monkeypatch.setenv(variable + "_SHA256", write(path, body))
    (runner.s_pilot if stage == "pilot" else runner.s_live_inference)()
    record = runner.report["stages"][stage]
    assert record["ok"] and not runner._stage_problems(stage, record)
    path.unlink()
    runner.report["target_identity"]["image_digests_sha256"] = "d" * 64
    assert any("deployment" in p for p in runner._stage_problems(stage, record))


@pytest.mark.parametrize("failure", ["absent", "hash", "wrong_candidate", "traversal", "consent", "unassisted", "stale"])
def test_invalid_pilot_stays_blocked(tmp_path, runner, monkeypatch, failure):
    path, body = packet(tmp_path, "pilot")
    row = body["sessions"][-1]
    trace = path.parent / row["result_file"]
    if failure in ("consent", "unassisted", "stale"):
        data = json.loads(trace.read_text())
        data.update({"consent": {"consent_ref": "mismatched"}, "unassisted": {"unassisted_tasks_1_to_3": [True, False, True]},
                     "stale": {"stale_action_refused": False}}[failure])
        row["result_sha256"] = write(trace, data)
    if failure == "wrong_candidate":
        body["candidate_sha256"] = "f" * 64
    if failure == "traversal":
        row["result_file"] = "../outside.json"
    monkeypatch.setenv("QUALIFY_PILOT_EVIDENCE_SHA256", write(path, body) if failure != "hash" else "0"*64)
    monkeypatch.setenv("QUALIFY_PILOT_EVIDENCE", str(path) if failure != "absent" else "")
    runner.s_pilot()
    assert runner.report["stages"]["pilot"]["ok"] is False


def test_hosted_trace_must_cover_the_same_run(tmp_path, runner, monkeypatch):
    path, body = packet(tmp_path, "live_inference_hosted")
    body["providers"]["bedrock"]["run_id"] = "different"
    monkeypatch.setenv("QUALIFY_HOSTED_EVIDENCE", str(path))
    monkeypatch.setenv("QUALIFY_HOSTED_EVIDENCE_SHA256", write(path, body))
    runner.s_live_inference()
    assert runner.report["stages"]["live_inference_hosted"]["ok"] is False


def test_external_manifest_integrity_is_rechecked_at_verdict(tmp_path, runner, monkeypatch):
    path, body = packet(tmp_path, "pilot")
    monkeypatch.setenv("QUALIFY_PILOT_EVIDENCE", str(path))
    monkeypatch.setenv("QUALIFY_PILOT_EVIDENCE_SHA256", write(path, body))
    runner.s_pilot()
    record = runner.report["stages"]["pilot"]
    original = (tmp_path / record["result_file"]).parent / "original.json"
    original.write_text("{}")
    assert any("changed" in p for p in runner._stage_problems("pilot", record))
