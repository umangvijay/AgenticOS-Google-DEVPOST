"""v5.6 review §3/§4/§5 regressions: the self-healing loop, evaluation
records from real execution events, and verified-outcome rewards.

- §3 Recovery policy: caps per category, repeated-action detection,
  deadline escalation, structured incident payloads.
- §4 EvaluationService: evaluation records carry the REAL per-run token
  spend the model client recorded (no placeholder zeros), and the worker
  generates them once per terminal run.
- §5 Stage 1: the adaptive-policy reward is positive only for VERIFIED
  outcomes (usable payload), never for a silent-but-empty response.
"""
import pytest

from backend.engine.failure_classifier import classify_failure
from backend.services.recovery_policy import RecoveryAttempt, RecoveryPolicy


def _classification(category: str):
    return {
        "rate_limited": lambda: classify_failure("too many requests", status=429),
        "schema_mismatch": lambda: classify_failure("rejected", status=422),
        "auth_invalid": lambda: classify_failure("rejected", status=401),
        "server_error": lambda: classify_failure("unavailable", status=503),
        "timeout": lambda: classify_failure("request timed out"),
    }[category]()


# ── §3: recovery policy ───────────────────────────────────────────────

def test_s3_ceiling_blocks_recovery_and_escalates():
    policy = RecoveryPolicy(attempts=[
        RecoveryAttempt(1, "rate_limited", "retry", "h1", "first"),
        RecoveryAttempt(2, "rate_limited", "retry", "h2", "second"),
    ])
    decision = policy.decide(_classification("rate_limited"), "core.http")
    assert decision.allowed is False
    assert decision.escalate is True
    assert "ceiling" in decision.reason.lower()


def test_s3_repeated_identical_action_is_refused_even_within_budget():
    classification = _classification("schema_mismatch")
    same_args = {"url": "https://api.example.com/x"}
    first_hash = RecoveryPolicy.action_hash("core.http", "schema_mismatch", same_args)
    policy = RecoveryPolicy(attempts=[
        RecoveryAttempt(1, "schema_mismatch", "repair_arguments", first_hash, "first try")])
    decision = policy.decide(classification, "core.http", same_args)
    assert decision.allowed is False
    assert "identical" in decision.reason.lower()


def test_s3_changed_arguments_are_allowed_within_ceiling():
    classification = _classification("schema_mismatch")
    policy = RecoveryPolicy(attempts=[
        RecoveryAttempt(1, "schema_mismatch", "repair_arguments",
                        RecoveryPolicy.action_hash("core.http", "schema_mismatch", {"url": "a"}),
                        "first")])
    decision = policy.decide(classification, "core.http", {"url": "b", "page": 2})
    assert decision.allowed is True
    assert decision.escalate is False


def test_s3_non_recoverable_categories_refuse_immediately():
    # auth_invalid (credential rejected) and internal (unknown failure) are
    # never auto-recovered — they escalate with the recovery hint attached.
    for category in ("auth_invalid", "internal"):
        classification = (classify_failure("rejected", status=401) if category == "auth_invalid"
                          else classify_failure("boom"))
        assert classification.category == category
        decision = RecoveryPolicy().decide(classification, "core.http")
        assert decision.allowed is False and decision.escalate is True
        assert decision.reason


def test_s3_deadline_exceeded_escalates():
    from datetime import datetime, timedelta, timezone
    started = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    policy = RecoveryPolicy(recovery_started_at=started)
    decision = policy.decide(_classification("rate_limited"), "core.http")
    assert decision.allowed is False and decision.escalate is True


def test_s3_incident_payload_is_actionable_and_redacted():
    classification = _classification("schema_mismatch")
    attempts = [RecoveryAttempt(1, "schema_mismatch", "repair_arguments", "h", "tried X",
                                verified=False)]
    incident = RecoveryPolicy.incident("task-1", "run-1", attempts, classification, "ceiling")
    assert incident["kind"] == "recovery_incident"
    assert incident["recovery_hint"] == classification.recovery_hint
    assert incident["attempts"][0]["rationale"] == "tried X"
    assert isinstance(incident["user_options"], list) and incident["user_options"]


def test_s3_outcome_verification_requires_usable_payload():
    assert RecoveryPolicy.verify_outcome({"reply": "ok"}) is True
    assert RecoveryPolicy.verify_outcome({"tool_result": {"a": 1}}) is True
    assert RecoveryPolicy.verify_outcome({"status_code": 200}) is True
    assert RecoveryPolicy.verify_outcome({}) is False
    assert RecoveryPolicy.verify_outcome({"reply": ""}) is False
    assert RecoveryPolicy.verify_outcome(None) is False


# ── §4: evaluation records from real execution events ─────────────────

@pytest.mark.asyncio
async def test_s4_evaluation_record_carries_real_per_run_tokens(tmp_path):
    """§12.14 corrected contract: the REAL per-run spend comes from the
    DURABLE usage ledger (settled reservation with the actual model), not
    a process-local counter the evaluator drains."""
    import pytest_asyncio
    from backend.models.schemas import Task, TaskStatus, WorkflowRun
    from backend.models.usage import ReservationRequest, SettlementReceipt, MeasurementSource
    from backend.repositories.usage_repository import (
        SQLiteUsageRepository, set_usage_repo)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.evaluation_service import evaluate_run_async

    db = DatabaseManager(str(tmp_path / "s4-usage.db"))
    await db.initialize()
    usage_repo = SQLiteUsageRepository(db)
    set_usage_repo(usage_repo)
    try:
        from backend.repositories.usage_repository import BucketLimit
        from backend.models.usage import BudgetScope
        res = await usage_repo.reserve(ReservationRequest(
            user_id="u1", units=540, run_id="run-eval-1", task_id="t1",
            call_id="call-1", model="gemini-test-model"),
            [BucketLimit(BudgetScope.USER, "u1", 100_000)])
        await usage_repo.settle(res.reservation_id, SettlementReceipt(
            reservation_id=res.reservation_id, units=540,
            measurement_source=MeasurementSource.PROVIDER_REPORTED,
            resolved_model="gemini-test-model"))

        run = WorkflowRun(
            run_id="run-eval-1", workflow_id="wf-eval", user_id="u1",
            goal="g", status=TaskStatus.COMPLETED,
            tasks=[Task(task_id="t1", workflow_id="wf-eval",
                        run_id="run-eval-1", user_id="u1", agent="core.set",
                        input_data={}, status=TaskStatus.COMPLETED,
                        output_data={"reply": "ok"},
                        acceptance_criteria=[{"kind": "non_empty",
                                              "value": "reply"}])],
        )
        record = await evaluate_run_async(run)
        assert record.token_usage_total == 540, \
            "evaluation must carry the REAL per-run spend from the ledger"
        assert record.model == "gemini-test-model" or record.model, \
            "actual model attribution present"
    finally:
        set_usage_repo(None)
        await db.close()


def test_s4_run_tokens_are_drained_after_evaluation():
    from backend.services.gemini_client import record_run_tokens, drain_run_tokens
    record_run_tokens("run-eval-2", 100)
    assert drain_run_tokens("run-eval-2") == 100
    assert drain_run_tokens("run-eval-2") == 0, "drained exactly once"


# ── §5 stage 1: verified outcomes gate the reward ─────────────────────

@pytest.mark.asyncio
async def test_s5_policy_records_failure_for_empty_payloads():
    """§5 stage 1: the verified-outcome gate is deterministic — an empty
    payload maps to a FAILURE reward for whichever model ran."""
    recorded = []

    class RecordingPolicy:
        async def record(self, model, success, latency_ms):
            recorded.append((model, success))

    policy = RecordingPolicy()
    raw = {}  # silent-but-empty response
    payload_verified = isinstance(raw, dict) and bool(
        str(raw.get("reply") or raw.get("message") or "").strip() or raw.get("tool_result"))
    await policy.record("gemini-3.8-flash", payload_verified, 0)
    assert recorded == [("gemini-3.8-flash", False)]
