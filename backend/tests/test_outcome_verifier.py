"""§9 Outcome Verifier regressions: bounded, read-only acceptance checking."""
import pytest

from backend.services.outcome_verifier import AcceptanceCriterion, OutcomeVerifier


def test_s9_verifier_passes_on_satisfied_criteria():
    """§10.8: artifact_exists now requires a resolver — the resolved artifact
    is the evidence, not a dict key."""
    resolver = {"resolver": lambda ref: ref == "att-9"}
    result = OutcomeVerifier().verify(
        {"reply": "Released v1.2 with notes", "artifact_id": "att-9"},
        [AcceptanceCriterion("contains", "v1.2"),
         AcceptanceCriterion("artifact_exists", resolver)],
    )
    assert result.passed and result.verdict == "pass"


def test_s9_verifier_flags_partial_failure_as_needs_review():
    result = OutcomeVerifier().verify(
        {"reply": "Released v1.2"},  # no artifact
        [AcceptanceCriterion("contains", "v1.2"), AcceptanceCriterion("artifact_exists")],
    )
    assert result.verdict == "needs_review"
    assert any("artifact" in r for r in result.reasons)


def test_s9_verifier_fails_when_everything_missing():
    result = OutcomeVerifier().verify(
        {"other": "x"},
        [AcceptanceCriterion("contains", "v1.2"), AcceptanceCriterion("artifact_exists")],
    )
    assert result.verdict == "fail"


def test_s9_verifier_fails_empty_output_without_criteria():
    result = OutcomeVerifier().verify({})
    assert result.verdict == "fail"
    assert "empty" in result.reasons[0]


def test_s9_verifier_is_read_only():
    """Contract: verification must not mutate the output it inspects."""
    output = {"reply": "x", "artifact_id": "a"}
    snapshot = dict(output)
    OutcomeVerifier().verify(output, [AcceptanceCriterion("artifact_exists")])
    assert output == snapshot


def test_s9_unknown_criterion_kind_is_a_failure_not_a_crash():
    result = OutcomeVerifier().verify(
        {"reply": "x"}, [AcceptanceCriterion("teleport", value="moon")],
    )
    assert result.verdict == "fail"
    assert any("unknown criterion" in r for r in result.reasons)
