"""A producer must not downgrade a mandatory stage's evidence contract."""
import hashlib
import json

import pytest

from scripts.qualify_release import parse_retained_counts


def receipt(tmp_path, body, kind="shell"):
    path = tmp_path / "result"
    path.write_text(body)
    return {"kind": kind, "ok": True, "exit": 0, "counts": {"passed": 1},
            "result_file": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("stage", ["restore", "target_deployment", "migrations",
                                   "performance", "pilot", "live_inference_hosted",
                                   "product_e2e_build", "product_e2e_browser"])
def test_stage_cannot_choose_pytest_parser(stage, tmp_path):
    record = receipt(tmp_path, '<testsuite><testcase name="unrelated"/></testsuite>', "pytest")
    assert parse_retained_counts(stage, record, tmp_path)


@pytest.mark.parametrize("stage", ["migrations", "pilot", "live_inference_hosted",
                                   "product_e2e_build", "product_e2e_lint", "product_e2e_typecheck"])
@pytest.mark.parametrize("body", [{}, {"ok": True}, {"exit": 0}])
def test_incomplete_receipt_is_not_success(stage, body, tmp_path):
    assert parse_retained_counts(stage, receipt(tmp_path, json.dumps(body)), tmp_path)


def test_command_receipt_cannot_be_reused_for_another_stage(tmp_path):
    body = {"stage": "product_e2e_lint", "ok": True, "exit": 0,
            "command": ["npm", "run", "lint"]}
    assert parse_retained_counts("product_e2e_build", receipt(tmp_path, json.dumps(body)), tmp_path)


@pytest.mark.parametrize("body", [
    {"ok": True, "exit": 0, "subchecks": []},
    {"ok": True, "exit": 0, "subchecks": {"dump": "ok"}},
])
def test_malformed_recovery_receipt_refuses_without_crashing(body, tmp_path):
    assert parse_retained_counts("restore", receipt(tmp_path, json.dumps(body)), tmp_path)


def test_unknown_stage_cannot_gain_a_generic_success(tmp_path):
    body = {"ok": True, "exit": 0, "stage": "unregistered"}
    assert parse_retained_counts("unregistered", receipt(tmp_path, json.dumps(body)), tmp_path)
