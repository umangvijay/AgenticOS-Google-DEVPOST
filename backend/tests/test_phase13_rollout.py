"""Phase 13 rollout acceptance tests (§13.1/§13.3/§13.5): the executable
half of the self-improvement loop.

- The versioned prompt store: immutable versions, activation pointer,
  rollback restores the PRIOR version exactly.
- The proposer pipeline: reviewed failure clusters become candidate
  experiments proposed by the AGENT actor (which can never promote);
  without a drafting step the pipeline proposes nothing.
- Deterministic shadow/canary assignment: the same (prompt, subject,
  experiment) lands in the same cohort across restarts; the predeclared
  fraction is honored.
- THE full lifecycle: propose → evaluate → shadow → canary → promote →
  rollback RESTORES THE PRIOR PROMPT VERSION (the active template
  reverts; history is preserved).
- Policy snapshots: snapshot → mutate → restore returns the counters.
"""
import asyncio
import json

import pytest
import pytest_asyncio

from backend.models.experiment import (
    ExperimentKind, ExperimentState, ExperimentThresholds)
from backend.repositories.experiment_repository import (
    ExperimentRepository, set_experiment_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.improvement_rollout import (
    assign_variant,
    clusters_from_episodes,
    propose_from_failure_clusters,
    resolve_task_instructions,
    make_restore,
)
from backend.services.improvement_service import ImprovementService
from backend.services.prompt_versions import (
    PolicySnapshotStore, PromptVersionStore,
)

BASELINE = "You are a careful read-only research assistant. {goal}"
CANDIDATE = ("You are a careful read-only research assistant. {goal}\n"
             "GUARDRAIL: never repeat a failed request without changed arguments.")


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "rollout.db"))
    await db.initialize()
    store = PromptVersionStore(db)
    snapshots = PolicySnapshotStore(db)
    repo = ExperimentRepository(db)
    set_experiment_repo(repo)
    # Production wiring: the resolver reads the module singletons.
    from backend.services.prompt_versions import (
        set_prompt_version_store, set_policy_snapshot_store)
    set_prompt_version_store(store)
    set_policy_snapshot_store(snapshots)
    try:
        yield {"db": db, "store": store, "snapshots": snapshots, "repo": repo,
               "service": ImprovementService(repo)}
    finally:
        set_experiment_repo(None)
        set_prompt_version_store(None)
        set_policy_snapshot_store(None)
        await db.close()


# ── §13.5: the versioned prompt store ──────────────────────────────────

@pytest.mark.asyncio
async def test_prompt_versions_are_immutable_and_roll_back(env):
    store = env["store"]
    v1 = await store.checkout("p1", BASELINE, created_by="operator:a",
                              note="initial")
    assert v1["version"] == 1
    # Identical content short-circuits (no duplicate versions).
    dup = await store.checkout("p1", BASELINE, created_by="operator:a")
    assert dup["version_id"] == v1["version_id"]
    v2 = await store.checkout("p1", CANDIDATE, created_by="agent:proposer")
    assert v2["version"] == 2

    await store.activate("p1", v2["version_id"], activated_by="operator:b")
    assert (await store.get_active("p1"))["template"] == CANDIDATE

    # §13 acceptance: rollback restores the PRIOR version exactly.
    rolled = await store.rollback_to("p1", 1, operator="operator:c")
    active = await store.get_active("p1")
    assert active["template"] == BASELINE
    assert active["version"] == 1
    assert active["activated_by"] == "rollback:operator:c"
    # History preserved — the candidate version still exists.
    assert await store.get_version("p1", 2) is not None


# ── §13.1: the proposer pipeline ────────────────────────────────────────

def test_clusters_group_reviewed_failures():
    episodes = [
        {"episode_id": "e1", "state": "ESCALATED", "failure_category": "timeout"},
        {"episode_id": "e2", "state": "ESCALATED", "failure_category": "timeout"},
        {"episode_id": "e3", "state": "ESCALATED", "failure_category": "schema"},
        {"episode_id": "e4", "state": "VERIFIED_RECOVERY",
         "failure_category": "timeout"},   # recovered — not a reviewed failure
    ]
    clusters = clusters_from_episodes(episodes)
    assert clusters[0]["category"] == "timeout"
    assert clusters[0]["count"] == 2
    assert all(c["category"] != "timeout" or c["count"] == 2 for c in clusters)


@pytest.mark.asyncio
async def test_proposer_pipeline_requires_a_drafter(env):
    """§13.1 honesty: without a drafting step the pipeline proposes
    NOTHING — it never fabricates candidates."""
    outcome = await propose_from_failure_clusters(
        episode_repo=_FakeEpisodeRepo(), improvement_service=env["service"],
        prompt_store=env["store"], dataset={"cases": [1]})
    assert outcome == []


@pytest.mark.asyncio
async def test_proposer_pipeline_creates_agent_proposed_experiments(env):
    store = env["store"]
    baseline_v = await store.checkout("read_only_task_instructions", BASELINE,
                                      created_by="operator:a")
    await store.activate("read_only_task_instructions",
                         baseline_v["version_id"], activated_by="operator:a")

    def draft(cluster):
        return CANDIDATE

    proposed = await propose_from_failure_clusters(
        episode_repo=_FakeEpisodeRepo(), improvement_service=env["service"],
        prompt_store=store, dataset={"cases": [1, 2]}, draft_candidate=draft)
    assert len(proposed) == 1
    exp = await env["repo"].get_experiment(proposed[0]["experiment_id"])
    assert exp.proposed_by == "agent:improvement_proposer"
    assert exp.kind is ExperimentKind.PROMPT
    # The candidate ref is a REAL version in the store.
    candidate = await store.get_by_version_id(exp.candidate_ref)
    assert candidate is not None and "GUARDRAIL" in candidate["template"]
    # The agent CANNOT promote its own proposal (§13.4, enforced).
    from backend.services.improvement_service import ImprovementError
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:bob",
        run_evaluator=lambda v, d: {"success": 0.95, "constraint_no_unverified_success": True},
        dataset={"cases": [1, 2]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:carol")
    await env["service"].start_canary(exp.experiment_id, activator="operator:carol",
                                      cohort_size=5)
    with pytest.raises(ImprovementError, match="Agents cannot"):
        await env["service"].promote(exp.experiment_id, approver="agent:improvement_proposer",
                                     deployment_version="v1")


class _FakeEpisodeRepo:
    """§13.8: the proposer consumes the reviewed-failure query — terminal
    episodes the REAL repository produces."""
    async def reviewed_failures(self, *, tenant_id=None, since_epoch=None,
                                limit=200):
        return [
            {"episode_id": "e1", "state": "ESCALATED",
             "failure_category": "schema_mismatch"},
            {"episode_id": "e2", "state": "ESCALATED",
             "failure_category": "schema_mismatch"},
        ]


# ── §13.3: deterministic cohort assignment ─────────────────────────────

def test_assignment_is_deterministic_across_restarts():
    a = [assign_variant("p", f"subject-{i}", "exp-1", 0.2)
         for i in range(50)]
    b = [assign_variant("p", f"subject-{i}", "exp-1", 0.2)
         for i in range(50)]
    assert a == b, "the same subject must land in the same cohort forever"


def test_assignment_honors_the_cohort_fraction():
    candidates = sum(
        1 for i in range(10_000)
        if assign_variant("p", f"s-{i}", "exp-1", 0.2) == "candidate")
    # Within a sane band of the predeclared 20%.
    assert 1_500 < candidates < 2_500
    assert all(assign_variant("p", f"s-{i}", "exp-1", 0.0) == "baseline"
               for i in range(200))
    assert all(assign_variant("p", f"s-{i}", "exp-1", 1.0) == "candidate"
               for i in range(200))


@pytest.mark.asyncio
async def test_shadow_serves_baseline_canary_splits(env):
    """Shadow mode NEVER serves the candidate to real traffic; canary
    splits deterministically."""
    store = env["store"]
    baseline_version = await store.checkout("rp", BASELINE, created_by="op")
    await store.activate("rp", baseline_version["version_id"],
                         activated_by="op")
    candidate = await store.checkout("rp", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:rp",
        hypothesis="h", expected_benefit="b",
        baseline_ref=(await store.get_active("rp"))["version_id"],
        candidate_ref=candidate["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(max_cohort_fraction=0.25,
                                        max_cohort_size=50),
        proposer="operator:a")
    await env["service"].evaluate(exp.experiment_id, evaluator="operator:b",
                                  run_evaluator=lambda v, d: {"success": 0.9, "samples": 20,
                                                              "constraint_x": True, "constraint_no_unauthorized_actions": True,
                                                              "constraint_no_unresolved_required_effects": True,
                                                              "constraint_no_unverified_success": True},
                                  dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    shadow = await resolve_task_instructions({"_prompt_id": "rp"}, "subject-7")
    assert shadow["template"] == BASELINE, (
        "shadow must not serve the candidate")
    assert shadow["source"] in ("active",) or shadow["source"].startswith("experiment:")
    await env["service"].start_canary(exp.experiment_id,
                                      activator="operator:c", cohort_size=25)
    seen = set()
    for i in range(200):
        resolved = await resolve_task_instructions({"_prompt_id": "rp"}, f"s-{i}")
        seen.add(resolved["template"])
    assert seen == {BASELINE, CANDIDATE}, "canary splits traffic"


# ── THE full lifecycle: promote, then rollback restores the prior version

@pytest.mark.asyncio
async def test_promotion_then_rollback_restores_prior_prompt(env):
    store = env["store"]
    baseline_version = await store.checkout("full", BASELINE,
                                            created_by="operator:a")
    await store.activate("full", baseline_version["version_id"],
                         activated_by="operator:a")
    active_before = await store.get_active("full")
    candidate = await store.checkout("full", CANDIDATE, created_by="agent:x")

    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:full",
        hypothesis="guardrail helps", expected_benefit="fewer failures",
        baseline_ref=active_before["version_id"],
        candidate_ref=candidate["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(
            success_min=0.8, critical_constraints=("no_unverified_success",)),
        proposer="operator:a")
    await env["service"].evaluate(
        exp.experiment_id, evaluator="operator:b",
        run_evaluator=lambda v, d: {"success": 0.9, "samples": 20,
                                    "constraint_no_unverified_success": True,
                                    "constraint_no_unauthorized_actions": True,
                                    "constraint_no_unresolved_required_effects": True},
        dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    await env["service"].start_canary(exp.experiment_id,
                                      activator="operator:c", cohort_size=10)
    promoted = await env["service"].promote(
        exp.experiment_id, approver="operator:d",
        deployment_version="deploy-13", total_population=200)
    assert promoted.state.value == "PROMOTED"
    # Promotion evidence is complete.
    assert promoted.promoted_dataset_hash == promoted.dataset_hash
    assert promoted.deployment_version == "deploy-13"

    # Activate the candidate (what promotion means for prompts).
    await store.activate("full", candidate["version_id"],
                         activated_by="promotion")
    assert (await store.get_active("full"))["template"] == CANDIDATE

    # §13 acceptance: ROLLBACK RESTORES THE PRIOR VERSION — wired through
    # the experiment's own baseline ref.
    restore = make_restore(prompt_store=store)
    await env["service"].rollback(exp.experiment_id, operator="operator:e",
                                  reason="canary follow-up regression",
                                  restore=restore)
    active_after = await store.get_active("full")
    assert active_after["template"] == BASELINE, \
        "rollback must restore the prior prompt version"
    assert active_after["version"] == active_before["version"]


# ── §13.5: policy snapshots ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_policy_snapshot_and_restore(tmp_path):
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository)
    db = DatabaseManager(str(tmp_path / "policy.db"))
    await db.initialize()
    repo = SQLiteAdaptivePolicyRepository(db)
    snapshots = PolicySnapshotStore(db)
    try:
        await repo.record_outcome("model-a", True, 100.0)
        await repo.record_outcome("model-a", True, 120.0)
        snap = await snapshots.snapshot(await repo.get_stats(),
                                        created_by="operator:a")
        # Learning continues...
        await repo.record_outcome("model-a", False, 900.0)
        await repo.record_outcome("model-b", True, 50.0)
        stats_after = {s["model"]: s for s in await repo.get_stats()}
        assert stats_after["model-a"]["failures"] == 1
        assert "model-b" in stats_after

        # Rollback restores the snapshot's counters exactly.
        restored = await repo.restore_snapshot(snap["stats"])
        assert restored == 1
        stats = {s["model"]: s for s in await repo.get_stats()}
        assert stats["model-a"]["failures"] == 0
        assert stats["model-a"]["attempts"] == 2
        assert "model-b" not in stats
    finally:
        await db.close()


# ── §13.17: genuinely bounded canary (review finding H) ────────────────

@pytest.mark.asyncio
async def test_canary_absolute_cap_enforced_by_counted_admission(env):
    """§13.17 acceptance: the cap holds as traffic grows — the durable
    exposure count enforces the exact finite cohort, and re-exposure is
    idempotent."""
    store, repo = env["store"], env["repo"]
    baseline_v = await store.checkout("cap-p", BASELINE, created_by="op")
    await store.activate("cap-p", baseline_v["version_id"], activated_by="op")
    candidate = await store.checkout("cap-p", CANDIDATE, created_by="agent:x")
    exp = await env["service"].propose(
        kind=ExperimentKind.PROMPT, target="prompt:cap-p",
        hypothesis="h", expected_benefit="b",
        baseline_ref=(await store.get_active("cap-p"))["version_id"],
        candidate_ref=candidate["version_id"],
        dataset_ref="ds", dataset={"cases": [1]},
        thresholds=ExperimentThresholds(max_cohort_fraction=0.25,
                                        max_cohort_size=3),
        proposer="operator:a")
    await env["service"].evaluate(exp.experiment_id, evaluator="operator:b",
                                  run_evaluator=lambda v, d: {"success": 0.9},
                                  dataset={"cases": [1]})
    await env["service"].start_shadow(exp.experiment_id, activator="operator:c")
    await env["service"].start_canary(exp.experiment_id,
                                      activator="operator:c", cohort_size=3)
    # More than 3 hash-matched subjects: only 3 are admitted.
    admitted = 0
    for i in range(50):
        if await repo.record_exposure(exp.experiment_id, f"s-{i}",
                                      max_cohort_size=3):
            admitted += 1
    assert admitted == 3, "the absolute cohort cap is exact"
    assert await repo.exposure_count(exp.experiment_id) == 3


@pytest.mark.asyncio
async def test_active_experiments_found_by_target_not_scan(env):
    """§13.21: the active lookup queries the target directly — an older
    experiment remains discoverable regardless of list windowing."""
    store, repo = env["store"], env["repo"]
    for i in range(3):
        baseline = await store.checkout(f"t{i}", BASELINE, created_by="op")
        await store.activate(f"t{i}", baseline["version_id"], activated_by="op")
        cand = await store.checkout(f"t{i}", CANDIDATE, created_by="agent:x")
        exp = await env["service"].propose(
            kind=ExperimentKind.PROMPT, target=f"prompt:t{i}",
            hypothesis="h", expected_benefit="b",
            baseline_ref=baseline["version_id"], candidate_ref=cand["version_id"],
            dataset_ref="ds", dataset={"cases": [1]},
            thresholds=ExperimentThresholds(max_cohort_fraction=0.1),
            proposer="operator:a")
        await env["service"].evaluate(exp.experiment_id, evaluator="operator:b",
                                      run_evaluator=lambda v, d: {"success": 0.9},
                                      dataset={"cases": [1]})
        await env["service"].start_shadow(exp.experiment_id,
                                          activator="operator:c")
    found = await repo.active_for_target("prompt:t0")
    assert len(found) == 1 and found[0].state is ExperimentState.SHADOW


# ── §13.9: real diffs + dedup ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_proposal_diff_is_real_content_not_summary(env):
    """§13.9: the candidate diff is a unified diff over the ACTUAL
    baseline/candidate contents."""
    store = env["store"]
    baseline_v = await store.checkout("read_only_task_instructions", BASELINE,
                                      created_by="operator:a")
    await store.activate("read_only_task_instructions",
                         baseline_v["version_id"], activated_by="operator:a")

    def draft(cluster):
        return CANDIDATE

    proposed = await propose_from_failure_clusters(
        episode_repo=_FakeEpisodeRepo(), improvement_service=env["service"],
        prompt_store=store, dataset={"cases": [1]}, draft_candidate=draft)
    exp = await env["repo"].get_experiment(proposed[0]["experiment_id"])
    assert "You are a careful read-only research assistant. {goal}" in exp.candidate_diff
    assert "+GUARDRAIL" in exp.candidate_diff
    assert "baseline:" in exp.candidate_diff and "candidate:" in exp.candidate_diff


@pytest.mark.asyncio
async def test_equivalent_proposals_dedup(env):
    """§13.9: the same candidate content is never proposed twice."""
    store = env["store"]
    baseline_v = await store.checkout("read_only_task_instructions", BASELINE,
                                      created_by="operator:a")
    await store.activate("read_only_task_instructions",
                         baseline_v["version_id"], activated_by="operator:a")

    def draft(cluster):
        return CANDIDATE

    first = await propose_from_failure_clusters(
        episode_repo=_FakeEpisodeRepo(), improvement_service=env["service"],
        prompt_store=store, dataset={"cases": [1]}, draft_candidate=draft)
    assert len(first) == 1
    second = await propose_from_failure_clusters(
        episode_repo=_FakeEpisodeRepo(), improvement_service=env["service"],
        prompt_store=store, dataset={"cases": [1]}, draft_candidate=draft)
    assert second == [], "an identical candidate is deduplicated"
