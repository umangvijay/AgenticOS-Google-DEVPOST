"""Improvement rollout (Phase 13 §13.1/§13.3/§13.5) — the executable half
of the self-improvement loop.

- §13.1: reviewed failure clusters (durable recovery episodes + failing
  evaluation verdicts) become CANDIDATE experiments through the proposer
  pipeline. Agents propose (actor "agent:*") — they can never approve or
  promote (enforced by the ImprovementService).
- §13.3: shadow/canary traffic assignment is DETERMINISTIC — a hash of
  (prompt, subject, experiment) decides baseline vs candidate, so the
  same task lands in the same cohort across restarts and replicas, and
  the cohort fraction is honored exactly.
- §13.5: rollback restores the PRIOR prompt version (pointer move) or
  policy snapshot — real versioned rollback, not a state label.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from backend.models.experiment import (
    ExperimentKind,
    ExperimentState,
    ExperimentThresholds,
    is_agent_actor,
)
from backend.services.prompt_versions import (
    PolicySnapshotStore, PromptVersionStore,
)

logger = logging.getLogger("improvement_rollout")

PROPOSER_ACTOR = "agent:improvement_proposer"

# Bounded: the proposer never floods the experiment queue (§13.1).
MAX_CANDIDATES_PER_RUN = 3
DEFAULT_TARGET_PROMPT = "read_only_task_instructions"


# ── §13.3: deterministic cohort assignment ─────────────────────────────

def assign_variant(prompt_id: str, subject_id: str, experiment_id: str,
                   cohort_fraction: float) -> str:
    """Return "candidate" or "baseline" — deterministic for the same
    (prompt, subject, experiment) triple, reproducible across restarts
    and replicas, and honoring the predeclared cohort fraction."""
    if cohort_fraction >= 1.0:
        return "candidate"
    if cohort_fraction <= 0.0:
        return "baseline"
    bucket = hashlib.sha256(
        f"{prompt_id}|{subject_id}|{experiment_id}".encode()).hexdigest()
    return "candidate" if int(bucket[:8], 16) % 10_000 < int(
        cohort_fraction * 10_000) else "baseline"


# ── §13.1: the proposer pipeline ────────────────────────────────────────

def clusters_from_episodes(episodes: List[Dict[str, Any]],
                           max_clusters: int = MAX_CANDIDATES_PER_RUN) -> List[Dict[str, Any]]:
    """Group reviewed failure episodes by category with counts and samples
    (the same grouping the improvement service summarizes)."""
    clusters: Dict[str, Dict[str, Any]] = {}
    for ep in episodes or []:
        if ep.get("state") not in ("ESCALATED", "RECOVERY_FAILED"):
            continue   # reviewed failures only (defensive; the query filters)
        cat = ep.get("failure_category") or ep.get("failure_code") or "unclassified"
        c = clusters.setdefault(cat, {"category": cat, "count": 0,
                                      "sample_episodes": []})
        c["count"] += 1
        if len(c["sample_episodes"]) < 5:
            c["sample_episodes"].append(ep.get("episode_id"))
    ranked = sorted(clusters.values(), key=lambda c: -c["count"])
    return ranked[:max_clusters]


async def propose_from_failure_clusters(
    episode_repo, improvement_service, prompt_store: PromptVersionStore, *,
    actor: str = PROPOSER_ACTOR,
    draft_candidate: Optional[Callable[[Dict[str, Any]], str]] = None,
    target_prompt_id: str = DEFAULT_TARGET_PROMPT,
    dataset: Any = None,
    dataset_ref: str = "failure-cluster-holdout",
) -> List[Dict[str, Any]]:
    """Turn reviewed failure clusters into candidate PROMPT experiments.

    The candidate template comes from `draft_candidate` (an LLM drafting
    step wired by the caller). Without one, the pipeline proposes NOTHING
    for that cluster — it never fabricates candidates. Agents propose
    under the "agent:*" actor; approval and promotion stay human."""
    from backend.models.admission import epoch_now
    if not is_agent_actor(actor):
        raise ValueError("The proposer pipeline runs as an agent actor")
    # §13.8: the REVIEWED-FAILURE query against the real repository —
    # terminal failed/escalated episodes. An active abandoned episode is a
    # recovery candidate, never an improvement candidate; escalation alone
    # does not imply review (the caller passes the review window).
    try:
        if hasattr(episode_repo, "reviewed_failures"):
            episodes = await episode_repo.reviewed_failures(limit=200)
        else:
            episodes = []
    except Exception:
        logger.debug("Reviewed-failure pull failed for the proposer",
                     exc_info=True)
        episodes = []
    reviewed = clusters_from_episodes(episodes)
    proposed: List[Dict[str, Any]] = []
    if not reviewed or draft_candidate is None or dataset is None:
        return proposed

    active = await prompt_store.get_active(target_prompt_id)
    baseline_ref = active["version_id"] if active else "baseline"
    thresholds = ExperimentThresholds(
        success_min=0.8, regression_max=0.0, max_cohort_fraction=0.1,
        max_cohort_size=50, critical_constraints=("no_unverified_success",))

    for cluster in reviewed:
        template = draft_candidate(cluster)
        if not template or not template.strip():
            continue
        candidate = await prompt_store.checkout(
            target_prompt_id, template, created_by=actor,
            note=f"candidate for {cluster['category']}")
        if candidate["version_id"] == baseline_ref:
            continue   # the draft is identical to the active version
        # §13.9: dedup — an experiment with the SAME candidate content
        # already exists (open or terminal): do not propose it again.
        try:
            existing = await improvement_service.repo.list_experiments(limit=200)
            if any(e.candidate_ref == candidate["version_id"]
                   for e in existing):
                logger.info("Proposer dedup: %s already proposed",
                            candidate["version_id"])
                continue
        except Exception:
            logger.debug("Proposal dedup scan failed", exc_info=True)
        try:
            # §13.9: the diff is generated from the ACTUAL contents —
            # never a hand-written summary string.
            import difflib as _difflib
            baseline_entry = await prompt_store.get_by_version_id(baseline_ref)
            baseline_text = (baseline_entry or {}).get("template", "")
            diff = "".join(_difflib.unified_diff(
                baseline_text.splitlines(keepends=True),
                template.splitlines(keepends=True),
                fromfile=f"baseline:{baseline_ref}",
                tofile=f"candidate:{candidate['version_id']}"))
            experiment = await improvement_service.propose(
                kind=ExperimentKind.PROMPT,
                target=f"prompt:{target_prompt_id}",
                hypothesis=(f"Guardrail section for '{cluster['category']}' "
                            f"failures ({cluster['count']} reviewed episodes) "
                            "reduces recurrence on read-only tasks"),
                expected_benefit=f"fewer {cluster['category']} recoveries",
                baseline_ref=baseline_ref,
                candidate_ref=candidate["version_id"],
                dataset_ref=dataset_ref,
                dataset=dataset,
                thresholds=thresholds,
                proposer=actor,
                candidate_diff=diff,
            )
            proposed.append({"experiment_id": experiment.experiment_id,
                             "cluster": cluster["category"]})
        except Exception as exc:
            # Permission/verification gates refuse loudly; the proposer
            # never retries past a refusal.
            logger.info("Proposer refused for cluster %s: %s",
                        cluster["category"], exc)
    return proposed


# ── §13.3: task-prompt resolution through the store ─────────────────────

async def resolve_task_instructions(
    task_input: Dict[str, Any], subject_id: str
) -> Optional[Dict[str, Any]]:
    """Resolve the instructions for one task execution through the version
    store. Returns {template, version_id, source} so the CALLER can
    persist the effective version with the attempt (§13.14) — never just
    a string whose provenance is lost.

    Precedence (§13.14): an experiment-assigned candidate OVERRIDES a
    stale `_instructions` input (the experiment claims this variant ran —
    silently keeping an older value would lie); with no experiment
    assignment, an explicit `_instructions` input wins over the stored
    active template."""
    prompt_id = (task_input or {}).get("_prompt_id")
    explicit = (task_input or {}).get("_instructions")
    if not prompt_id:
        if explicit:
            return {"template": explicit, "version_id": None,
                    "source": "task_input"}
        return None
    try:
        from backend.services.prompt_versions import get_prompt_version_store
        store = get_prompt_version_store()
        if store is None:
            return {"template": explicit, "version_id": None,
                    "source": "task_input"} if explicit else None
        active = await store.get_active(prompt_id)
        if not active:
            return {"template": explicit, "version_id": None,
                    "source": "task_input"} if explicit else None
        variant_template = active["template"]
        variant_version_id = active["version_id"]
        variant_source = "active"
        try:
            from backend.repositories.experiment_repository import (
                get_experiment_repo)
            exp_repo = get_experiment_repo()
            if exp_repo is not None:
                # §13.21: query active experiments for THIS target directly.
                experiments = await exp_repo.active_for_target(
                    f"prompt:{prompt_id}")
                for exp in experiments:
                    thresholds = exp.thresholds_json
                    import json as _json
                    t = _json.loads(thresholds or "{}")
                    fraction = (float(t.get("max_cohort_fraction", 0.1))
                                if exp.state is ExperimentState.CANARY else 0.0)
                    if assign_variant(prompt_id, subject_id,
                                      exp.experiment_id, fraction) != "candidate":
                        continue
                    # §13.17: the DURABLE absolute cap — admission is
                    # counted; the hash fraction only pre-screens.
                    admitted = await exp_repo.record_exposure(
                        exp.experiment_id, subject_id,
                        max_cohort_size=int(t.get("max_cohort_size", 50)))
                    if not admitted:
                        continue   # cohort full — serve the baseline
                    candidate = await store.get_by_version_id(
                        exp.candidate_ref)
                    if candidate:
                        variant_template = candidate["template"]
                        variant_version_id = candidate["version_id"]
                        variant_source = (f"experiment:{exp.experiment_id}")
                    break
        except Exception:
            logger.debug("Cohort resolution failed; serving the active "
                         "version", exc_info=True)
        if variant_source == "active" and explicit:
            # No experiment assignment: the caller's explicit instructions
            # take precedence over the stored template.
            return {"template": explicit, "version_id": None,
                    "source": "task_input"}
        return {"template": variant_template,
                "version_id": variant_version_id, "source": variant_source}
    except Exception:
        logger.debug("Prompt resolution failed for %s", prompt_id,
                     exc_info=True)
        return None


def _version_from_ref(ref: str) -> int:
    """candidate_ref is a version_id (pv_...) in the store; the numeric
    version travels alongside in experiments as a fallback."""
    try:
        return int(str(ref).split(":v")[-1])
    except ValueError:
        return -1


# ── §13.5: rollback wiring ──────────────────────────────────────────────

def make_restore(prompt_store: Optional[PromptVersionStore] = None,
                 policy_store: Optional[PolicySnapshotStore] = None,
                 policy_repo=None) -> Callable[[Any], Any]:
    """Build the `restore` callback for ImprovementService.rollback: a
    PROMOTED prompt experiment rolls back to its baseline version; a
    POLICY experiment restores the latest durable snapshot."""

    async def restore(experiment) -> None:
        if experiment.kind is ExperimentKind.PROMPT and prompt_store:
            prompt_id = experiment.target.split("prompt:", 1)[-1]
            baseline = experiment.baseline_ref
            if baseline.startswith("pv_"):
                await prompt_store.activate(prompt_id, baseline,
                                            activated_by="experiment-rollback")
            else:
                version = _version_from_ref(baseline)
                if version > 0:
                    await prompt_store.rollback_to(prompt_id, version,
                                                   operator="experiment-rollback")
        elif experiment.kind is ExperimentKind.POLICY and policy_store and policy_repo:
            snap = await policy_store.latest()
            if snap:
                await policy_repo.restore_snapshot(snap["stats"])
        else:
            raise ValueError(
                f"No versioned store wired for experiment kind {experiment.kind}")

    return restore
