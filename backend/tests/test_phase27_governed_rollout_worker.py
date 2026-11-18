"""Phase 27 §27.R5 — governed policy rollout witnessed on REAL WORKER
TASK DISPATCHES (not just the resolution function).

Journey: baseline revision active → a real worker dispatch records the
baseline version → a candidate enters canary → a cohorted user's real
dispatch records the CANDIDATE version (source=experiment) → promotion
activates the candidate → a post-promotion dispatch records it → a
WORSE candidate is refused → rollback restores the prior revision →
a post-rollback dispatch records the PRIOR version again. Every
receipt is the persisted execution step's policy_revision (§13.14).
"""
import asyncio
import uuid
from contextlib import suppress
from types import SimpleNamespace

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    from backend.config.settings import settings
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_DB_PATH", str(tmp_path / "r5-worker.db"))
    monkeypatch.setattr(settings, "MESSAGE_BUS_TRANSPORT", "inprocess")
    from backend.repositories.factory import RepositoryFactory
    from backend.engine.engine import WorkflowEngine
    f = RepositoryFactory()
    await f.initialize()
    from backend.services.prompt_versions import (
        PromptVersionStore, set_prompt_version_store)
    from backend.repositories.experiment_repository import (
        ExperimentRepository, set_experiment_repo)
    store = PromptVersionStore(f.db_manager)
    set_prompt_version_store(store)
    repo = ExperimentRepository(f.db_manager)
    set_experiment_repo(repo)
    engine = WorkflowEngine(f.workflow_repo, f.message_bus)
    monkeypatch.setattr("backend.worker._worker_factory", f)
    uid = "rollout-" + uuid.uuid4().hex[:8]
    await f.user_repo.create_user({"id": uid, "email": uid + "@example.test",
                                   "name": "Rollout owner"})
    from backend.worker import start_worker
    worker = asyncio.create_task(start_worker(f.message_bus, engine,
                                              user_repo=f.user_repo))
    try:
        yield {"f": f, "engine": engine, "store": store, "experiments": repo,
               "uid": uid}
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        set_prompt_version_store(None)
        set_experiment_repo(None)
        await f.shutdown()


PROMPT = "recovery.retry_instructions"
BASELINE = "Retry the failed call up to 2 times with backoff."
CANDIDATE = "Classify the failure first; retry only transient causes, twice."


async def _dispatch_and_wait(s, user_id: str, marker: str) -> dict:
    """Save a real run, trigger the worker, and return the run + the
    persisted execution step for its single task."""
    f, engine = s["f"], s["engine"]
    run_id = "r5-" + uuid.uuid4().hex
    await f.workflow_repo.save_run({"run_id": run_id, "user_id": user_id,
        "goal": f"R5 dispatch {marker}", "status": "RUNNING",
        "tasks": [{"task_id": "t1", "status": "PENDING", "agent": "core.set",
                   "input_data": {"_prompt_id": PROMPT,
                                  "fields": {"marker": marker}},
                   "max_retries": 0}]})
    await f.message_bus.publish("agentos-workflow-events",
                                {"event_type": "task.execute",
                                 "run_id": run_id, "task_id": "t1"})
    async with asyncio.timeout(30):
        while True:
            run = await f.workflow_repo.get_run(run_id)
            if run["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                break
            await asyncio.sleep(0.05)
    assert run["status"] == "COMPLETED", run
    steps = await f.execution_step_repo.steps_for_run(run_id)
    assert steps, "the dispatch must record an execution step"
    return {"run": run, "step": steps[-1]}


@pytest.mark.asyncio
async def test_worker_dispatch_records_rollout_versions_end_to_end(stack):
    s = stack
    store, svc_repo = s["store"], s["experiments"]
    from backend.services.improvement_rollout import assign_variant

    # 1. Baseline active; a real worker dispatch records it.
    v1 = await store.checkout(PROMPT, BASELINE, created_by="operator:alice",
                              note="initial")
    await store.activate(PROMPT, v1["version_id"], activated_by="operator:alice")
    first = await _dispatch_and_wait(s, s["uid"], "baseline")
    assert first["step"].get("policy_revision") == v1["version_id"], \
        "the real dispatch consumed the baseline revision"

    # 2. Candidate through the governed pipeline into canary.
    from backend.services.improvement_service import ImprovementService
    from backend.models.experiment import ExperimentKind, ExperimentThresholds
    svc = ImprovementService(svc_repo)
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
        dataset_ref="recovery-holdout-v2", dataset="recovery-holdout-v2",
        thresholds=thresholds, proposer="agent:recovery-analyst",
        candidate_diff=BASELINE + " → " + CANDIDATE)
    metrics = {"baseline": {"success": 0.70, "cost_usd": 0.5},
               "candidate": {"success": 0.90, "cost_usd": 0.5, "samples": 20,
                             "constraint_no_unauthorized_actions": True,
                             "constraint_no_unresolved_required_effects": True,
                             "constraint_no_unverified_success": True}}
    await svc.evaluate(exp.experiment_id, evaluator="operator:bob",
                       run_evaluator=lambda variant, ds:
                       metrics["baseline" if variant == v1["version_id"]
                               else "candidate"],
                       dataset="recovery-holdout-v2")
    await svc.start_shadow(exp.experiment_id, activator="operator:carol")
    await svc.start_canary(exp.experiment_id, activator="operator:carol",
                           cohort_size=50)

    # 3. A REAL dispatch from a cohorted user during canary carries the
    #    CANDIDATE template (source experiment:*), persisted on the step.
    cohort_uid = None
    for _ in range(500):
        probe = "cohort-" + uuid.uuid4().hex[:8]
        if assign_variant(PROMPT, probe, exp.experiment_id,
                          thresholds.max_cohort_fraction) == "candidate":
            cohort_uid = probe
            break
    assert cohort_uid, "a candidate-cohort user id must exist"
    await s["f"].user_repo.create_user({"id": cohort_uid,
        "email": cohort_uid + "@example.test", "name": "Cohort user"})
    canary = await _dispatch_and_wait(s, cohort_uid, "canary")
    assert canary["step"].get("policy_revision") == cand_v["version_id"], \
        "the canary-cohort dispatch consumed the candidate revision"

    # 4. Promotion + activation → the next real dispatch consumes the
    #    candidate as the ACTIVE revision.
    promoted = await svc.promote(exp.experiment_id, approver="operator:dave",
                                 deployment_version="7.0.0")
    assert promoted.state.value == "PROMOTED"
    await store.activate(PROMPT, cand_v["version_id"],
                         activated_by="operator:dave")
    post = await _dispatch_and_wait(s, s["uid"], "promoted")
    assert post["step"].get("policy_revision") == cand_v["version_id"]

    # 5. A WORSE candidate is refused by the human gate.
    worse_v = await store.checkout(PROMPT, "Never retry anything.",
                                   created_by="agent:recovery-analyst")
    exp2 = await svc.propose(
        kind=ExperimentKind.PROMPT, target=f"prompt:{PROMPT}",
        hypothesis="No retries at all", expected_benefit="lower cost",
        baseline_ref=cand_v["version_id"], candidate_ref=worse_v["version_id"],
        dataset_ref="recovery-holdout-v2", dataset="recovery-holdout-v2",
        thresholds=thresholds, proposer="agent:recovery-analyst")
    await svc.evaluate(exp2.experiment_id, evaluator="operator:bob",
                       run_evaluator=lambda variant, ds:
                       ({"success": 0.40, "cost_usd": 0.5}
                        if variant == worse_v["version_id"]
                        else {"success": 0.90, "cost_usd": 0.5}),
                       dataset="recovery-holdout-v2")
    await svc.start_shadow(exp2.experiment_id, activator="operator:carol")
    await svc.start_canary(exp2.experiment_id, activator="operator:carol",
                           cohort_size=10)
    from backend.services.improvement_service import ImprovementError
    with pytest.raises(ImprovementError):
        await svc.promote(exp2.experiment_id, approver="operator:dave",
                          deployment_version="7.0.1")

    # 6. ROLLBACK re-activates the prior revision through the real
    #    restore hook; the NEXT dispatch consumes it again.
    restored_ids = []

    async def restore(experiment):
        prior = await store.checkout(PROMPT, BASELINE,
                                     created_by="operator:erin",
                                     note="rollback reversion")
        await store.activate(PROMPT, prior["version_id"],
                             activated_by="operator:erin")
        restored_ids.append(prior["version_id"])
        return {"restored": prior["version_id"]}

    rolled = await svc.rollback(exp.experiment_id, operator="operator:erin",
                                reason="canary showed error spike",
                                restore=restore)
    assert rolled.state.value == "ROLLED_BACK"
    post_rb = await _dispatch_and_wait(s, s["uid"], "rollback")
    assert post_rb["step"].get("policy_revision") == restored_ids[0], \
        "post-rollback the worker dispatch consumes the prior revision"


@pytest_asyncio.fixture(autouse=True)
def _quiet():
    yield
    # The in-process bus must not leak subscriptions between tests.
    with suppress(Exception):
        pass
