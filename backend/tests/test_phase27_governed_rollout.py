"""Phase 27 §27.R5 — governed policy rollout and rollback on the WORKER's
policy-resolution path.

The journey: a real worker decision under the BASELINE revision → a
candidate is approved and promoted to canary → a cohorted decision under
the CANDIDATE revision → a worse candidate is REJECTED → rollback →
post-rollback decision under the PRIOR revision. Every decision records
its effective version (§13.14) — never just a string.
"""
import uuid

import pytest
import pytest_asyncio

from backend.services.improvement_rollout import (
    assign_variant, resolve_task_instructions)


@pytest_asyncio.fixture
async def stack(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.prompt_versions import (
        PromptVersionStore, set_prompt_version_store)
    from backend.repositories.experiment_repository import (
        ExperimentRepository, set_experiment_repo)
    db = DatabaseManager(str(tmp_path / "rollout.db"))
    await db.initialize()
    store = PromptVersionStore(db)
    set_prompt_version_store(store)
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    try:
        yield {"store": store, "repo": repo, "db": db}
    finally:
        set_prompt_version_store(None)
        set_experiment_repo(None)
        await db.close()


PROMPT = "recovery.retry_instructions"
BASELINE = "Retry the failed call up to 2 times with backoff."
CANDIDATE = "Classify the failure first; retry only transient causes, twice."


@pytest.mark.asyncio
async def test_worker_decision_under_baseline_then_candidate_then_rollback(
        stack):
    store, repo = stack["store"], stack["repo"]
    subject = f"worker-{uuid.uuid4().hex[:8]}"

    # 1. Baseline revision is checked out and activated.
    v1 = await store.checkout(PROMPT, BASELINE, created_by="operator:alice",
                              note="initial")
    await store.activate(PROMPT, v1["version_id"], activated_by="operator:alice")

    # 2. Worker decision #1 — under the PRIOR (baseline) revision.
    pre = await resolve_task_instructions({"_prompt_id": PROMPT}, subject)
    assert pre is not None and pre["source"] == "active"
    assert pre["template"] == BASELINE
    assert pre["version_id"] == v1["version_id"], \
        "the worker records the effective version with the attempt (§13.14)"

    # 3. A candidate is proposed through the governed pipeline: proposed
    #    with a real candidate version → evaluated (beats baseline) →
    #    human-approved → promoted.
    from backend.services.improvement_service import ImprovementService
    from backend.models.experiment import ExperimentKind, ExperimentThresholds
    svc = ImprovementService(repo)
    dataset = "recovery-holdout-v2"
    cand_v = await store.checkout(PROMPT, CANDIDATE,
                                  created_by="agent:recovery-analyst",
                                  note="candidate")
    thresholds = ExperimentThresholds(
        success_min=0.8, regression_max=0.0, regression_metrics=("success",),
        cost_max_usd=1.0, max_cohort_fraction=0.1, max_cohort_size=50)
    exp = await svc.propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:{PROMPT}",
        hypothesis="Failure-classified retries reduce wasted attempts",
        expected_benefit="+20% verified recovery on the held-out set",
        baseline_ref=v1["version_id"], candidate_ref=cand_v["version_id"],
        dataset_ref=dataset, dataset=dataset, thresholds=thresholds,
        proposer="agent:recovery-analyst",
        candidate_diff=BASELINE + " → " + CANDIDATE)

    def run_evaluator(variant, ds):
        # variant is the ref string: candidate_ref → candidate metrics.
        return metrics["baseline" if variant == v1["version_id"]
                       else "candidate"]

    metrics = {"baseline": {"success": 0.70, "cost_usd": 0.5},
               "candidate": {"success": 0.90, "cost_usd": 0.5, "samples": 20,
                             "constraint_no_unauthorized_actions": True,
                             "constraint_no_unresolved_required_effects": True,
                             "constraint_no_unverified_success": True}}
    await svc.evaluate(exp.experiment_id, evaluator="operator:bob",
                       run_evaluator=run_evaluator, dataset=dataset)
    await svc.start_shadow(exp.experiment_id, activator="operator:carol")
    await svc.start_canary(exp.experiment_id, activator="operator:carol",
                           cohort_size=50)
    # 4. Worker decisions during CANARY: the cohort assignment is a
    #    deterministic hash — scan subjects until one is assigned the
    #    candidate; its decision resolves the CANDIDATE revision and
    #    records a counted exposure (§13.18).
    canary_subject = None
    for i in range(200):
        cand_subject = f"worker-{uuid.uuid4().hex[:8]}"
        if assign_variant(PROMPT, cand_subject, exp.experiment_id,
                          thresholds.max_cohort_fraction) == "candidate":
            canary_subject = cand_subject
            break
    assert canary_subject, "a candidate-cohort subject must exist"
    canary_dec = await resolve_task_instructions(
        {"_prompt_id": PROMPT}, canary_subject)
    assert canary_dec["template"] == CANDIDATE
    assert canary_dec["source"].startswith("experiment:")
    # Non-cohort subjects keep the baseline during canary.
    for i in range(200):
        base_subject = f"worker-{uuid.uuid4().hex[:8]}"
        if assign_variant(PROMPT, base_subject, exp.experiment_id,
                          thresholds.max_cohort_fraction) == "baseline":
            base_dec = await resolve_task_instructions(
                {"_prompt_id": PROMPT}, base_subject)
            assert base_dec["template"] == BASELINE
            break

    # Promotion AUTHORIZES deployment; the operator then ACTIVATES the
    # candidate version (the reconcilable activation receipt).
    promoted = await svc.promote(exp.experiment_id, approver="operator:dave",
                                 deployment_version="7.0.0")
    assert promoted.state.value == "PROMOTED"
    await store.activate(PROMPT, cand_v["version_id"],
                         activated_by="operator:dave")

    # 4b. Worker decision #2 — under the CANDIDATE revision (active).
    post = await resolve_task_instructions({"_prompt_id": PROMPT}, subject)
    assert post["template"] == CANDIDATE
    assert post["version_id"] == cand_v["version_id"]
    assert post["source"] == "active"

    # 5. A WORSE candidate is rejected explicitly (human gate).
    worse_v = await store.checkout(PROMPT, "Never retry anything.",
                                   created_by="agent:recovery-analyst")
    exp2 = await svc.propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:{PROMPT}",
        hypothesis="No retries at all",
        expected_benefit="lower cost",
        baseline_ref=cand_v["version_id"], candidate_ref=worse_v["version_id"],
        dataset_ref=dataset, dataset=dataset, thresholds=thresholds,
        proposer="agent:recovery-analyst")

    def worse_evaluator(variant, ds):
        if variant == worse_v["version_id"]:
            return {"success": 0.40, "cost_usd": 0.5}
        return {"success": 0.90, "cost_usd": 0.5}

    await svc.evaluate(exp2.experiment_id, evaluator="operator:bob",
                       run_evaluator=worse_evaluator, dataset=dataset)
    await svc.start_shadow(exp2.experiment_id, activator="operator:carol")
    await svc.start_canary(exp2.experiment_id, activator="operator:carol",
                           cohort_size=10)
    from backend.services.improvement_service import ImprovementError
    with pytest.raises(ImprovementError):
        await svc.promote(exp2.experiment_id, approver="operator:dave",
                          deployment_version="7.0.1")

    # 6. ROLLBACK the promoted candidate: the worker's NEXT decision
    #    resolves under the PRIOR (candidate-becomes-baseline) revision.
    restore_calls = []

    async def restore(experiment):
        restore_calls.append(1)
        # The REAL restore hook: re-activate the prior revision.
        prior = await store.checkout(PROMPT, BASELINE,
                                     created_by="operator:erin",
                                     note="rollback reversion")
        await store.activate(PROMPT, prior["version_id"],
                             activated_by="operator:erin")
        return {"restored": prior["version_id"]}

    rolled = await svc.rollback(exp.experiment_id, operator="operator:erin",
                                reason="canary showed error spike",
                                restore=restore)
    assert rolled.state.value == "ROLLED_BACK"
    assert restore_calls == [1], "the real restore hook ran exactly once"

    # 7. Worker decision #3 — post-rollback, under the PRIOR revision.
    post_rb = await resolve_task_instructions({"_prompt_id": PROMPT}, subject)
    assert post_rb["template"] == BASELINE, \
        "post-rollback the worker resolves the prior revision again"
    assert post_rb["version_id"] != cand_v["version_id"]
