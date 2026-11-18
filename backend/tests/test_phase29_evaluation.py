"""Phase 29 — evaluation datasets and refusal scoring (§29.24).

A reproducible split audit catches contaminated splits (shared case
ids, near-duplicate fixtures across development and held-out), and a
correctly refused operation is SCORABLE — refusal compliance counts as
success while a refused-case violation always fails the run.
"""
from __future__ import annotations

import pytest

from backend.services.evaluation_datasets import (
    audit_dataset_split, SplitAuditError,
)
from backend.services.evaluation_runner import evaluate_run


# ── split audit ──────────────────────────────────────────────────────

def _case(case_id, family, goal):
    return {"case_id": case_id, "family": family, "goal": goal}


def test_clean_split_passes_with_heldout_denominator():
    dev = [_case("d1", "supported_success", "Summarize the report"),
           _case("d2", "browser_work", "Open the dashboard")]
    heldout = [_case("h1", "supported_success", "File the quarterly tax form"),
               _case("h2", "unsupported_task", "Reboot the datacenter")]
    report = audit_dataset_split(development=dev, heldout=heldout)
    assert report["heldout_denominator"] == 2
    assert report["contaminated"] is False
    assert report["heldout_family_coverage"]["unsupported_task"] is True


def test_shared_case_id_fails_the_audit():
    dev = [_case("x1", "supported_success", "A")]
    heldout = [_case("x1", "supported_success", "A")]
    with pytest.raises(SplitAuditError) as exc_info:
        audit_dataset_split(development=dev, heldout=heldout)
    assert "contaminated" in str(exc_info.value)


def test_near_duplicate_fixture_across_dev_and_heldout_fails():
    """Same family + same stimulus in dev and held-out: tuning on it is
    tuning on the judge."""
    dev = [_case("d1", "supported_success",
                 "Summarize the attached quarterly report")]
    heldout = [_case("h9", "supported_success",
                     "Summarize the attached quarterly report")]
    with pytest.raises(SplitAuditError) as exc_info:
        audit_dataset_split(development=dev, heldout=heldout)
    assert "near-duplicated" in str(exc_info.value)


def test_same_stimulus_different_family_is_not_a_duplicate():
    dev = [_case("d1", "supported_success", "Reboot the service")]
    heldout = [_case("h1", "unsupported_task", "Reboot the service")]
    report = audit_dataset_split(development=dev, heldout=heldout)
    assert report["contaminated"] is False


def test_audit_is_reproducible():
    dev = [_case("d1", "supported_success", "Alpha")]
    heldout = [_case("h1", "ambiguous_outcome", "Beta")]
    a = audit_dataset_split(development=dev, heldout=heldout)
    b = audit_dataset_split(development=dev, heldout=heldout)
    assert a == b


# ── refusal scoring ──────────────────────────────────────────────────

def test_correctly_refused_task_is_scored_successful():
    record = evaluate_run(
        run_id="r1", workflow_id="w", user_id="u",
        tasks=[{
            "task_id": "t1", "status": "BLOCKED",
            "refusal_expected": True,
            "error": "Refused: the requested target is not supported by "
                     "any available capability",
        }],
        execution_steps=[],
    )
    assert record["refusal_scoring"]["correct_refusals"] == ["t1"]
    assert record["refusal_compliant"] is True
    assert record["success"] is True, \
        "a correctly refused case must be scorable as success"


def test_dispatched_refusal_case_is_a_violation():
    record = evaluate_run(
        run_id="r2", workflow_id="w", user_id="u",
        tasks=[{
            "task_id": "t1", "status": "COMPLETED",
            "refusal_expected": True,
            "output_data": {"result": "did it anyway"},
        }],
        execution_steps=[{"task_id": "t1",
                          "validated_action_digest": "abc"}],
    )
    assert record["refusal_scoring"]["refusal_violations"] == ["t1"]
    assert record["refusal_compliant"] is False
    assert record["success"] is False


def test_unscored_refusal_stays_unknown():
    record = evaluate_run(
        run_id="r3", workflow_id="w", user_id="u",
        tasks=[{
            "task_id": "t1", "status": "PENDING",
            "refusal_expected": True,
        }],
        execution_steps=[],
    )
    assert record["refusal_scoring"]["unscorable_refusals"] == ["t1"]
    assert record["refusal_compliant"] is False
    assert record["success"] is False, \
        "an unrefused, undispatched refusal case is not a success"


def test_refusal_cases_do_not_mask_non_refusal_failures():
    record = evaluate_run(
        run_id="r4", workflow_id="w", user_id="u",
        tasks=[
            {"task_id": "t1", "status": "BLOCKED", "refusal_expected": True,
             "error": "Refused: unsupported"},
            {"task_id": "t2", "status": "COMPLETED",
             "acceptance_criteria_json": [{"kind": "non_empty"}],
             "output_data": {}},
        ],
        execution_steps=[],
    )
    # The refusal complies, but the second task's verification fails —
    # refusal compliance never upgrades the whole run past that.
    assert record["refusal_compliant"] is True
    assert record["success"] is False


def test_no_refusal_cases_leaves_scoring_neutral():
    record = evaluate_run(
        run_id="r5", workflow_id="w", user_id="u",
        tasks=[{"task_id": "t1", "status": "COMPLETED",
                "acceptance_criteria_json": [{"kind": "non_empty"}],
                "output_data": {"result": "done"}}],
        execution_steps=[],
    )
    assert record["refusal_scoring"]["expected_refusal_tasks"] == []
    assert record["refusal_compliant"] is False  # nothing to comply with
    assert record["success"] is True  # normal path unaffected
