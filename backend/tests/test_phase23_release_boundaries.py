"""Unit regression evidence for the Phase 23 audit findings."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.qualify_release import Qualifier, REQUIRED_STAGES, junit_counts, frontend_counts
from backend.services.browser_execution import (
    BrowserExecutionService, ManagedBrowserSession, BrowserExecutionError,
    StaleControllerError, NotControllerError,
)


def qualifier(stages):
    q = Qualifier.__new__(Qualifier)
    q.strict = False
    q.report = {"stages": stages}
    return q


@pytest.mark.parametrize("stages", [{}, {"migrations": {"ok": True}}])
def test_incomplete_qualification_never_passes(stages):
    assert not qualifier(stages).verdict()[0]


@pytest.mark.parametrize("bad", [
    {"kind": "skip", "blocker": "known", "mandatory": True},
    {"kind": "pytest", "ok": True, "counts": {"passed": 2, "skipped": 1}},
    {"kind": "pytest", "ok": True, "counts": {}},
    {"kind": "partial", "ok": True},
])
def test_mandatory_gate_failure_is_not_waived(bad):
    stages = {name: {"kind": "shell", "ok": True} for name in REQUIRED_STAGES}
    stages["unit_contract"] = bad
    assert not qualifier(stages).verdict()[0]


def test_source_change_invalidates_attempt(tmp_path, monkeypatch):
    from scripts import qualify_release
    from backend.tests.test_phase25_verdict_evidence import base_report
    monkeypatch.setattr(qualify_release, "ATTEMPTS", tmp_path)
    q = Qualifier.__new__(Qualifier)
    q.report = base_report(tmp_path)
    assert q.verdict()[0]
    q.report["source_changed"] = True
    assert not q.verdict()[0]


def test_junit_counts_nested_suites_once(tmp_path):
    p = tmp_path / "results.xml"
    p.write_text('<testsuites><testsuite><testcase name="ok"/>'
                 '<testcase name="skip"><skipped/></testcase>'
                 '<testcase name="fail"><failure/></testcase></testsuite></testsuites>')
    assert junit_counts(p) == dict(passed=1, skipped=1, failed=1, errors=0)


@pytest.fixture
def browser():
    svc = BrowserExecutionService()
    s = ManagedBrowserSession("session", "owner", "run", "task",
                              ["https://example.com"], "agent", "epoch-1")
    s._page = SimpleNamespace(url="https://example.com", click=AsyncMock(),
                              inner_text=AsyncMock(return_value="observed"),
                              screenshot=AsyncMock(return_value=b"png"))
    svc._sessions[s.session_id] = s
    return svc, s


@pytest.mark.asyncio
async def test_missing_epoch_refused_after_takeover(browser):
    svc, s = browser
    await svc.request_takeover("owner", s.session_id)
    with pytest.raises(StaleControllerError):
        await svc.execute("owner", s.session_id, "click", "#send")
    s._page.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_epoch_does_not_grant_wrong_actor_control(browser):
    svc, s = browser
    await svc.request_takeover("owner", s.session_id)
    with pytest.raises(NotControllerError):
        await svc.execute("owner", s.session_id, "click", "#send",
                          controller_epoch=s.controller_epoch)
    await svc.execute("owner", s.session_id, "click", "#send",
                      controller_epoch=s.controller_epoch, actor_controller_id="user:owner")
    s._page.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_return_requires_agent_observation_not_viewer_frame(browser):
    svc, s = browser
    await svc.request_takeover("owner", s.session_id)
    await svc.return_control("owner", s.session_id)
    await svc.capture_frame("owner", s.session_id)
    with pytest.raises(BrowserExecutionError, match="fresh observation"):
        await svc.execute("owner", s.session_id, "click", "#send",
                          controller_epoch=s.controller_epoch)
    await svc.execute("owner", s.session_id, "extract_text", controller_epoch=s.controller_epoch)
    await svc.execute("owner", s.session_id, "click", "#send", controller_epoch=s.controller_epoch)
    s._page.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_takeover_serializes_with_admitted_action(browser):
    svc, s = browser
    started, release = asyncio.Event(), asyncio.Event()
    async def click(*args, **kwargs):
        started.set()
        await release.wait()
    s._page.click.side_effect = click
    action = asyncio.create_task(svc.execute("owner", s.session_id, "click", "#send",
                                             controller_epoch="epoch-1"))
    await started.wait()
    handoff = asyncio.create_task(svc.request_takeover("owner", s.session_id))
    await asyncio.sleep(0)
    assert not handoff.done()
    release.set()
    await action
    await handoff
    with pytest.raises(StaleControllerError):
        await svc.execute("owner", s.session_id, "click", "#send", controller_epoch="epoch-1")


def test_evaluation_exact_schedule_and_truncation(monkeypatch):
    from backend.learning import real_recovery_env as env_module
    from backend.learning.train_real_recovery_policy import evaluate
    resets, closes = [], []
    class Env:
        def reset(self, *, seed):
            resets.append(seed)
            return [], {}
        def step(self, action):
            return [], 0, False, True, {"task_class": "truncated", "verified_solved": False}
        def close(self):
            closes.append(True)
    monkeypatch.setattr(env_module, "RealRecoveryEnv", Env)
    first = evaluate(None, seeds=(1, 2, 3), episodes_per_seed=12)
    assert first == evaluate(None, seeds=(1, 2, 3), episodes_per_seed=12)
    assert first["episodes"] == 36 and first["solved"] == 0
    assert len(set(resets[:36])) == 36 and len(closes) == 6


@pytest.mark.parametrize("limits", [{"network": True}, {"allowed_destinations": ["https://example.com"]}])
def test_network_grants_refused_without_enforcement(limits):
    from backend.runners.container_runner import ContainerRunner, ContainerLimits
    runner = ContainerRunner.__new__(ContainerRunner)
    with pytest.raises(ValueError, match="egress broker"):
        runner.run("id", "echo ignored", limits=ContainerLimits(**limits))


def fake_runner(chunks, store=None):
    from backend.runners.container_runner import ContainerRunner
    runner = ContainerRunner.__new__(ContainerRunner)
    runner._image = "mutable:tag"
    runner._image_digest = "sha256:verified"
    runner._records = {}
    runner._containers = {}
    runner._record_store = store
    api = SimpleNamespace(exec_create=Mock(return_value={"Id": "exec"}),
                          exec_start=Mock(return_value=iter(chunks)),
                          exec_inspect=Mock(return_value={"ExitCode": 0}))
    container = SimpleNamespace(id="container-12345", client=SimpleNamespace(api=api),
                                remove=Mock(), stop=Mock())
    runner._client = SimpleNamespace(containers=SimpleNamespace(run=Mock(return_value=container)))
    return runner, container


def test_runner_pins_image_caps_bytes_and_records_cleanup_failure():
    from backend.runners.container_runner import ContainerLimits
    writes = []
    runner, container = fake_runner([("€€€".encode(), b"stderr")],
                                    SimpleNamespace(put=lambda _, data: writes.append(data)))
    container.remove.side_effect = RuntimeError("daemon unavailable")
    result = runner.run("execution", "echo ignored", limits=ContainerLimits(max_output_bytes=5))
    launch = runner._client.containers.run.call_args
    assert launch.args[0] == "sha256:verified"
    assert launch.kwargs["network_disabled"] is True
    assert launch.kwargs["cap_drop"] == ["ALL"] and launch.kwargs["read_only"]
    assert len(result.stdout.encode()) <= 5 and result.truncated
    assert result.cleanup_state == "orphaned" and result.cleanup_error
    assert len(writes) == 3 and writes[0]["container_id"] is None
    assert writes[-1]["cleanup_state"] == "orphaned"


def test_runner_durable_admission_failure_prevents_launch():
    store = SimpleNamespace(put=Mock(side_effect=OSError("database unavailable")))
    runner, _ = fake_runner([], store)
    with pytest.raises(OSError):
        runner.run("execution", "echo must-not-run")
    runner._client.containers.run.assert_not_called()


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../escape", "a\\escape"])
def test_runner_input_paths_cannot_escape(path):
    runner, _ = fake_runner([])
    with pytest.raises(ValueError, match="workspace"):
        runner.run("execution", "echo ignored", files={path: "data"})
    runner._client.containers.run.assert_not_called()


@pytest.mark.asyncio
async def test_browser_rejects_private_dns_answers(browser, monkeypatch):
    svc, _ = browser
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", AsyncMock(return_value=[
        (2, 1, 6, "", ("169.254.169.254", 80))]))
    with pytest.raises(BrowserExecutionError, match="private"):
        await svc._validate_destination("https://public-looking.example/")


def test_frontend_structured_results_preserve_skips_and_flakes(tmp_path):
    import json
    path = tmp_path / "playwright.json"
    path.write_text(json.dumps({"stats": {"expected": 4, "unexpected": 0,
                                        "skipped": 2, "flaky": 1}, "errors": []}))
    assert frontend_counts(path, "playwright") == {
        "passed": 4, "failed": 0, "skipped": 2, "flaky": 1, "errors": 0}
    stages = {name: {"ok": True} for name in REQUIRED_STAGES}
    stages["product_e2e_browser"] = {"ok": True, "kind": "shell", "result_file": str(path),
                                      "counts": frontend_counts(path, "playwright")}
    assert not qualifier(stages).verdict()[0]


def test_subset_and_retry_preserve_first_attempt_and_full_summary(tmp_path, monkeypatch):
    import json
    import sys
    from scripts import qualify_release as module
    monkeypatch.setattr(module, "ATTEMPTS", tmp_path / "attempts")
    monkeypatch.setattr(module, "REPORT", tmp_path / "full.json")
    monkeypatch.setattr(module, "git_identity", lambda: {"content_sha256": "source"})
    monkeypatch.setattr(module, "runtime_identity", lambda: {"profile": "unit-test"})
    module.REPORT.write_text('{"previous_full_attempt": true}')
    calls = []
    def migrations(self):
        calls.append(1)
        self.record("migrations", {"kind": "shell", "ok": len(calls) > 1})
    monkeypatch.setattr(Qualifier, "s_migrations", migrations)
    monkeypatch.setattr(sys, "argv", ["qualify", "--stage", "migrations", "--retry-record", "migrations:retry1"])
    assert module.main() == 1
    reports = list(module.ATTEMPTS.glob("*/report.json"))
    assert len(reports) == 1
    saved = json.loads(reports[0].read_text())
    assert saved["stages"]["migrations"]["ok"] is False
    assert saved["retries"]["retry1"]["results"]["migrations"]["ok"] is True
    assert saved["scope"] == "subset" and not saved["verdict"]["pass"]
    assert json.loads(module.REPORT.read_text()) == {"previous_full_attempt": True}
