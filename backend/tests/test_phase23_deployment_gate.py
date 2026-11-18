"""Deployment must bind current bytes, runtime, suite, profile, and results."""
import pytest

from backend.services.artifact_attestation_service import issue_attestation, verify_attestation
from backend.services.mcp_protocol_validator import REQUIRED_PROTOCOL_CASES, suite_digest
from backend.services.mcp_deployment_service import evaluate_deployment_eligibility


def evidence(**overrides):
    values = dict(artifact_digest="artifact", runtime_image_digest="image",
                  runner_profile_digest="profile", test_suite_digest=suite_digest(),
                  test_run_id="test-run", issuer_identity="trusted-validator",
                  observed_results={case["case"]: "passed" for case in REQUIRED_PROTOCOL_CASES})
    values.update(overrides)
    return issue_attestation(**values)


def deployment(**overrides):
    values = dict(artifact={"content_digest": "artifact", "runtime_image_digest": "image",
                            "protocol_validation_status": "passed", "isolation_validation_status": "passed"},
                  attestations=[evidence()], owner_user_id="owner", caller_user_id="owner",
                  content_digest_now="artifact", expected_runner_profile_digest="profile",
                  isolation_profile_verified=True, operation_eligibility=[{"tool_name": "read", "allowed": True}])
    values.update(overrides)
    return evaluate_deployment_eligibility(**values)


def test_full_current_evidence_is_eligible():
    assert deployment()["allowed"]


@pytest.mark.parametrize("override", [
    {"content_digest_now": None}, {"content_digest_now": "changed"},
    {"isolation_profile_verified": False}, {"expected_runner_profile_digest": None},
    {"expected_runner_profile_digest": "changed"}, {"operation_eligibility": []},
    {"budget_available": False}, {"connector_revoked": True},
])
def test_missing_or_changed_current_authority_blocks(override):
    assert not deployment(**override)["allowed"]


@pytest.mark.parametrize("override", [
    {"test_suite_digest": "obsolete-suite"},
    {"observed_results": {"initialize": "passed"}},
    {"observed_results": {case["case"]: "failed" for case in REQUIRED_PROTOCOL_CASES}},
    {"runtime_image_digest": "different-image"},
])
def test_valid_signature_does_not_authorize_wrong_or_incomplete_tests(override):
    attestation = evidence(**override)
    assert verify_attestation(attestation)
    assert not deployment(attestations=[attestation])["allowed"]


def test_malformed_signature_is_refused():
    assert not verify_attestation({"signature": 123})


def test_production_requires_configured_signing_key(monkeypatch):
    from backend.services import artifact_attestation_service as service
    from backend.config.settings import settings
    monkeypatch.setattr(service, "_ATTESTATION_KEY", None)
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.delenv("ATTESTATION_SIGNING_KEY", raising=False)
    with pytest.raises(ValueError, match="shared ATTESTATION_SIGNING_KEY"):
        evidence()
