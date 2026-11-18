"""Round-3 acceptance tests: model judges (§12.19), run replay (§21.3),
and recovery outbox intents (§12.5)."""
import json

import pytest

from backend.models.routing_decision import RoutingDecision  # noqa: F401
from backend.services.model_judge import (
    JUDGE_ELIGIBLE_KINDS, combine_verdicts, judge_harness_score, run_judge,
)
from backend.services.outcome_verifier import (
    AcceptanceCriterion, OutcomeVerifier,
)
from backend.services.workflow_templates import replay_run


# ── §12.19: model judges ───────────────────────────────────────────────

def test_verifier_marks_judge_kinds_needs_review_not_fail():
    """semantic_match/readability await a judge — UNVERIFIABLE (needs_
    review), never a hard fail and never a silent pass."""
    result = OutcomeVerifier().verify(
        {"reply": "ok"},
        [AcceptanceCriterion(kind="semantic_match", value="a polite reply")])
    assert result.verdict == "needs_review"


def test_judge_cannot_touch_authoritative_kinds():
    """§12.19 acceptance: a judge's confident explanation cannot validate
    a nonexistent artifact or authorize an action — authoritative kinds
    are refused outright."""
    result = run_judge("artifact_exists", {"resolver": None}, {},
                       judge_client=lambda p: '{"verdict": "pass"}')
    assert result.verdict == "needs_review"
    assert "not judge-eligible" in result.reasoning


def test_judge_recommendation_is_recorded_with_provenance():
    captured = {}

    def fake_judge(prompt):
        captured["prompt"] = prompt
        return json.dumps({"verdict": "pass",
                           "reasoning": "satisfies the criterion"})

    result = run_judge("semantic_match", "a polite reply",
                       {"reply": "thanks"}, judge_client=fake_judge,
                       model="judge-test-model")
    assert result.verdict == "pass"
    assert result.model == "judge-test-model"
    assert result.rubric_version
    assert result.prompt_digest, "the prompt version is recorded"
    assert "criterion" in captured["prompt"].lower()


def test_judge_output_without_judge_stays_needs_review():
    result = run_judge("readability", "clear prose", {"reply": "ok"})
    assert result.verdict == "needs_review"
    assert "no judge model configured" in result.reasoning


def test_unusable_judge_output_is_needs_review():
    result = run_judge("semantic_match", "x", {"reply": "y"},
                       judge_client=lambda p: "I feel great about this!")
    assert result.verdict == "needs_review"
    assert "unusable" in result.reasoning


def test_combine_rule_judge_cannot_upgrade_a_fail():
    from backend.services.model_judge import JudgeResult
    assert combine_verdicts("fail", JudgeResult(
        verdict="pass", reasoning="r", model="m")) == "fail"
    assert combine_verdicts("pass", JudgeResult(
        verdict="pass", reasoning="r", model="m")) == "pass"
    assert combine_verdicts("pass", JudgeResult(
        verdict="fail", reasoning="r", model="m")) == "needs_review"
    assert combine_verdicts("needs_review", None) == "needs_review"


def test_labeled_harness_gates_judge_usage():
    """§12.19: judges are tested against labeled examples; the ADVERSARIAL
    case (injection in the output) must resolve to needs_review — a
    rubber-stamping judge fails the harness."""
    def honest_judge(prompt):
        if "Ignore previous instructions" in prompt:
            return json.dumps({"verdict": "needs_review",
                               "reasoning": "output contains instructions"})
        if "INVALID INPUT" in prompt:
            return json.dumps({"verdict": "fail",
                               "reasoning": "not a polite reply"})
        return json.dumps({"verdict": "pass", "reasoning": "polite"})

    out = judge_harness_score(honest_judge)
    assert out["agreement"] == 1.0, out

    def rubber_stamp(prompt):
        return json.dumps({"verdict": "pass", "reasoning": "looks fine"})

    out = judge_harness_score(rubber_stamp)
    assert out["agreement"] < 1.0, "a rubber-stamping judge fails the harness"


# ── §21.3: run replay — inspection only ────────────────────────────────

def test_replay_reports_what_would_revalidate():
    steps = [
        {"step_id": "s1", "proposed_action": "search",
         "validated_action_digest": "dig-1", "state": "VERIFIED"},
        {"step_id": "s2", "proposed_action": "submit",
         "validated_action_digest": "dig-2", "state": "OUTCOME_UNCERTAIN",
         "error_code": "LOST_RESPONSE"},
        {"step_id": "s3", "proposed_action": "click", "state": "FAILED",
         "error_code": "INVALID_ARGUMENTS"},
    ]
    out = replay_run(steps, evaluation={"record": {"success": False,
                                                   "evaluator_version": "3.0.0"}})
    assert out["replay"] == "inspection-only"
    assert out["steps_total"] == 3
    assert out["steps_revalidating"] == 1, \
        "only the clean validated step would revalidate under current policy"
    by_id = {s["step_id"]: s for s in out["steps"]}
    assert by_id["s2"]["recorded_state"] == "OUTCOME_UNCERTAIN"
    assert out["recorded_evaluation"]["note"].startswith("the recorded "
                                                         "evaluation is historical")
    assert "No external effect is ever repeated" in out["note"]


# ── §12.5: recovery episodes emit durable outbox intents ───────────────

@pytest.mark.asyncio
async def test_recovery_transition_emits_outbox_intent(tmp_path):
    """§12.5: 'durable transitions AND outbox intents' — a protected
    recovery state change records an outbox event so downstream
    notification/publication survives a crash between the two steps."""
    from backend.models.admission import epoch_now
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository, RecoveryState)
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "recovery-outbox.db"))
    await db.initialize()
    repo = RecoveryEpisodeRepository(db)
    try:
        ep = await repo.create_episode({
            "episode_id": "ep-ob", "tenant_id": "u1", "actor_id": "system",
            "run_id": "run-ob", "task_id": "t1", "failure_code": "TIMEOUT",
            "goal_digest": "gd", "deadline_at": epoch_now() + 900})
        await repo.claim_episode("ep-ob", "w1")
        await repo.transition("ep-ob", RecoveryState.CLASSIFIED, actor="system")
        await repo.transition("ep-ob", RecoveryState.PROPOSED, actor="agent",
                              require_live_lease=True,
                              expected_owner_generation=1,
                              evidence_reference="repair queued")
        cursor = await db.execute(
            "SELECT payload_json FROM outbox_events WHERE "
            "event_type LIKE 'recovery%' ORDER BY created_at")
        rows = await cursor.fetchall()
        assert rows, "the recovery transition emitted an outbox intent"
        payload = json.loads(rows[-1]["payload_json"])
        assert payload["episode_id"] == "ep-ob"
    finally:
        await db.close()
