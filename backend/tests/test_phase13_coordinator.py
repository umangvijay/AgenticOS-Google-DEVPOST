"""Phase 13 chat-4/5 acceptance tests (§13.19/§13.20/§13.21).

THE central review finding F: promotion through the PUBLIC API must
change the effective runtime (the candidate actually activates), and
rollback through the API must restore the exact pinned baseline — with
the receipt distinguishing requested from confirmed. Plus §13.21
concurrency guards (a stale coordinator cannot overwrite a newer
release).
"""
import pytest
import pytest_asyncio

from backend.models.experiment import ExperimentKind, ExperimentState, ExperimentThresholds
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_service import ImprovementService
from backend.services.prompt_versions import (
    PromptVersionStore, set_prompt_version_store)
from backend.services.promotion_coordinator import (
    CoordinatorError, promote_and_activate, rollback_via_api,
)

BASELINE = "BASE {goal}"
CANDIDATE = "CAND {goal}"


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "coordinator.db"))
    await db.initialize()
    store = PromptVersionStore(db)
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    set_prompt_version_store(store)
    try:
        yield {"db": db, "store": store, "repo": repo,
               "service": ImprovementService(repo)}
    finally:
        set_experiment_repo(None)
        set_prompt_version_store(None)
        await db.close()


async def _canary_ready(env, prompt_id="p-live"):
    """A promoted-eligible experiment: propose → evaluate → shadow →
    canary, with the candidate checked out and the baseline ACTIVE."""
    store, service = env["store"], env["service"]
    baseline = await store.checkout(prompt_id, BASELINE, created_by="operator:a")
    await store.activate(prompt_id, baseline["version_id"], activated_by="operator:a")
    candidate = await store.checkout(prompt_id, CANDIDATE, created_by="agent:x")
    exp = await service.propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:{prompt_id}",
        hypothesis="h", expected_benefit="b",
        baseline_ref=baseline["version_id"], candidate_ref=candidate["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(success_min=0.8,
                                        critical_constraints=("safe",)),
        proposer="operator:a")
    await service.evaluate(exp.experiment_id, evaluator="operator:b",
                           run_evaluator=lambda v, d: {
                               "success": 0.9, "samples": 30,
                               "constraint_safe": True,
                               "constraint_no_unauthorized_actions": True,
                               "constraint_no_unresolved_required_effects": True,
                               "constraint_no_unverified_success": True},
                           dataset={"cases": [1]})
    await service.start_shadow(exp.experiment_id, activator="operator:c")
    await service.start_canary(exp.experiment_id, activator="operator:c",
                               cohort_size=5)
    return exp, baseline, candidate


# ── §13.19: promotion → effective activation ───────────────────────────

@pytest.mark.asyncio
async def test_api_promotion_changes_the_effective_runtime(env):
    """THE §13.19 acceptance: promoting via the coordinator activates the
    candidate — subsequent tasks consume it WITHOUT any manual store
    call."""
    exp, baseline, candidate = await _canary_ready(env)
    out = await promote_and_activate(
        env["service"], env["store"], exp.experiment_id,
        approver="operator:d", deployment_version="deploy-13.1",
        total_population=200)
    receipt = out["activation_receipt"]
    assert receipt["state"] == "effective"
    active = await env["store"].get_active("p-live")
    assert active["version_id"] == candidate["version_id"], \
        "the candidate is the EFFECTIVE runtime version"
    assert active["activated_by"].startswith("promotion:")


@pytest.mark.asyncio
async def test_activation_failure_is_a_visible_receipt_not_silence(env):
    """A crash/refusal between promotion and activation is reconcilable —
    the receipt names the state."""
    exp, baseline, candidate = await _canary_ready(env, prompt_id="p-fail")
    # Sabotage the STORE's activation target after the manifest pins a
    # real version: delete the candidate version row (simulates a lost
    # activation target) — the manifest still verifies (hash pinned at
    # proposal), the ACTIVATION fails visibly.
    await env["db"].execute(
        "DELETE FROM prompt_versions WHERE version_id=?",
        (candidate["version_id"],))
    await env["db"].commit()
    out = await promote_and_activate(
        env["service"], env["store"], exp.experiment_id,
        approver="operator:d", deployment_version="v1",
        total_population=200)
    assert out["activation_receipt"]["state"] == "failed"
    assert out["activation_receipt"]["error"]
    assert (await env["store"].get_active("p-fail"))["version_id"] == \
        baseline["version_id"], "the baseline still serves"


# ── §13.20: API rollback restores the exact baseline ───────────────────

@pytest.mark.asyncio
async def test_api_rollback_restores_the_pinned_baseline(env):
    """§13.20 acceptance: rollback through the coordinator reverts the
    EFFECTIVE runtime to the pinned baseline and reports CONFIRMED."""
    exp, baseline, candidate = await _canary_ready(env, prompt_id="p-rb")
    await promote_and_activate(env["service"], env["store"], exp.experiment_id,
                               approver="operator:d",
                               deployment_version="v1", total_population=200)
    assert (await env["store"].get_active("p-rb"))["template"] == CANDIDATE

    out = await rollback_via_api(env["service"], env["store"],
                                 exp.experiment_id, operator="operator:e",
                                 reason="canary follow-up regression")
    assert out["rollback_confirmed"] is True, \
        "rollback-CONFIRMED means the effective runtime reverted"
    active = await env["store"].get_active("p-rb")
    assert active["version_id"] == baseline["version_id"]
    assert active["template"] == BASELINE


@pytest.mark.asyncio
async def test_rollback_refuses_when_a_newer_release_is_live(env):
    """§13.20/§13.21: a newer release is never overwritten by a stale
    rollback."""
    exp, baseline, candidate = await _canary_ready(env, prompt_id="p-new")
    await promote_and_activate(env["service"], env["store"], exp.experiment_id,
                               approver="operator:d",
                               deployment_version="v1", total_population=200)
    # A NEWER release lands on top.
    newer = await env["store"].checkout("p-new", "NEWER {goal}",
                                        created_by="operator:f")
    await env["store"].activate("p-new", newer["version_id"],
                                activated_by="operator:f")
    with pytest.raises(CoordinatorError, match="newer release"):
        await rollback_via_api(env["service"], env["store"],
                               exp.experiment_id, operator="operator:e",
                               reason="late rollback")
    assert (await env["store"].get_active("p-new"))["version_id"] == \
        newer["version_id"]


@pytest.mark.asyncio
async def test_stale_coordinator_cannot_overwrite_a_newer_release(env):
    """§13.21: the expected-active-version guard — a coordinator whose
    snapshot is stale refuses instead of silently replacing."""
    exp, baseline, candidate = await _canary_ready(env, prompt_id="p-stale")
    with pytest.raises(CoordinatorError, match="newer release|changed"):
        await promote_and_activate(
            env["service"], env["store"], exp.experiment_id,
            approver="operator:d", deployment_version="v1",
            total_population=200,
            expected_active_version_id="pv_someone_elses")
