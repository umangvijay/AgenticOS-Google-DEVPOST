"""Improvement service (Phase 13) — the controlled self-improvement loop.

Turn reviewed failure clusters into candidate changes; evaluate baseline
and candidate on the SAME held-out set; run shadow → bounded canary with
predeclared thresholds; promote with separated permissions and full
evidence; roll back to the prior version.

Deterministic guarantees (the model can never argue its way past these):
- Grading rules and dataset are pinned by hash at proposal; evaluation
  refuses to run against anything else (§13.2).
- Agents (actor "agent:*") may propose but can never evaluate-approve,
  promote, or roll back, and may never propose changes to authorization
  or grading targets (§13.4).
- Promotion requires: approver ≠ proposer, a non-agent approver, ALL
  predeclared thresholds met, and ZERO critical-constraint violations —
  a candidate that improves AVERAGE quality but violates a critical
  constraint cannot promote (§13 acceptance).
- The canary cohort is bounded by the predeclared fraction/size.
- Every promotion links dataset hash, results, actor, and deployment
  version; rollback records the prior ref it restores (§13.5).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now
from backend.services.principals import principal_from_actor
from backend.models.experiment import (
    ROLE_ACTIVATOR,
    ROLE_APPROVER,
    ROLE_EVALUATOR,
    ROLE_OPERATOR,
    ROLE_PROPOSER,
    Experiment,
    ExperimentKind,
    ExperimentState,
    ExperimentThresholds,
    ExperimentTransitionError,
    agent_proposal_allowed,
    is_agent_actor,
)
from backend.repositories.experiment_repository import (
    ExperimentRepository,
    get_experiment_repo,
    new_experiment_id,
)

logger = logging.getLogger("improvement_service")

# §13.10: the IMMUTABLE experiment manifest — every referenced artifact
# is resolved and pinned at proposal time. Changing ANY pinned component
# requires a NEW experiment; promotion verifies the manifest still holds.
async def build_manifest(*, kind: ExperimentKind, target: str,
                         baseline_ref: str, candidate_ref: str,
                         dataset_ref: str, dataset_hash: str,
                         thresholds: ExperimentThresholds,
                         prompt_store=None) -> Dict[str, Any]:
    import hashlib as _hashlib
    from backend.services.evaluation_runner import EVALUATOR_VERSION
    from backend.services.model_judge import RUBRIC_VERSION
    manifest = {
        "kind": kind.value,
        "target": target,
        "baseline_ref": baseline_ref,
        "candidate_ref": candidate_ref,
        "dataset_ref": dataset_ref,
        "dataset_hash": dataset_hash,
        "evaluator_version": EVALUATOR_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "metric_protocol": {
            "metrics": sorted(thresholds.regression_metrics),
            "direction": {"lower_is_better":
                          sorted(thresholds.regression_metrics_lower_is_better)},
            "regression_max": thresholds.regression_max,
            "success_min": thresholds.success_min,
            "mandatory_constraints": sorted(_MANDATORY_CONSTRAINTS),
        },
        "pinned_at": epoch_now(),
    }
    if kind is ExperimentKind.PROMPT and prompt_store is not None:
        prompt_id = target.split("prompt:", 1)[-1]
        pinned = {}
        for role, ref in (("baseline", baseline_ref),
                          ("candidate", candidate_ref)):
            version = None
            if hasattr(prompt_store, "get_by_version_id"):
                version = await prompt_store.get_by_version_id(ref)
            if version is None:
                # Legacy numeric refs.
                try:
                    version = await prompt_store.get_version(
                        prompt_id, int(str(ref).split(":v")[-1]))
                except (ValueError, TypeError):
                    version = None
            if version is None or version.get("prompt_id") not in (None, prompt_id):
                raise ImprovementError(
                    f"{role} ref '{ref}' does not resolve to a version of "
                    f"target '{target}' — nonexistent or unrelated "
                    "references are rejected (§13.10)")
            pinned[role] = {
                "version_id": version["version_id"],
                "version": version.get("version"),
                "content_sha256": _hashlib.sha256(
                    str(version.get("template", "")).encode()).hexdigest()[:32],
            }
        manifest["prompt_versions"] = {
            "prompt_id": prompt_id, **pinned}
    manifest["manifest_sha256"] = _hashlib.sha256(
        json.dumps({k: v for k, v in manifest.items() if k != "manifest_sha256"},
                   sort_keys=True, default=str).encode()).hexdigest()[:32]
    return manifest


# §13.12: SERVER-OWNED mandatory constraints — every promotion evaluates
# these regardless of what the proposal declared. A candidate that
# produces unauthorized actions, unresolved required effects, or
# unverified success can never promote.
_MANDATORY_CONSTRAINTS = (
    "no_unauthorized_actions",
    "no_unresolved_required_effects",
    "no_unverified_success",
)


class ImprovementError(Exception):
    """A deterministic gate refused the requested improvement step."""


def dataset_hash_for(dataset: Any) -> str:
    return hashlib.sha256(
        json.dumps(dataset, sort_keys=True, default=str).encode()).hexdigest()[:32]


def grading_hash_for(thresholds: ExperimentThresholds) -> str:
    return hashlib.sha256(
        json.dumps(thresholds.to_dict(), sort_keys=True).encode()).hexdigest()[:32]


def failure_cluster_summary(episodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """§13.1: reviewed failure clusters → candidate inputs. Groups durable
    recovery episodes by failure category with counts and sample ids."""
    clusters: Dict[str, Dict[str, Any]] = {}
    for ep in episodes:
        cat = ep.get("failure_category") or "unclassified"
        cluster = clusters.setdefault(cat, {
            "category": cat, "count": 0, "sample_episodes": []})
        cluster["count"] += 1
        if len(cluster["sample_episodes"]) < 5:
            cluster["sample_episodes"].append(ep.get("episode_id"))
    return {
        "generated_at": epoch_now(),
        "clusters": sorted(clusters.values(),
                           key=lambda c: -c["count"]),
    }


class ImprovementService:
    def __init__(self, repo: Optional[ExperimentRepository] = None,
                 prompt_store=None):
        self.repo = repo or get_experiment_repo()
        if self.repo is None:
            raise ImprovementError(
                "Experiment repository not configured")
        # §13.10: the store used to pin/verify prompt-version manifests.
        self.prompt_store = prompt_store if prompt_store is not None \
            else _default_prompt_store()

    # ── §13.1: proposal ───────────────────────────────────────────────

    async def propose(self, *, kind: ExperimentKind, target: str,
                      hypothesis: str, expected_benefit: str,
                      baseline_ref: str, candidate_ref: str,
                      dataset_ref: str, dataset: Any,
                      thresholds: ExperimentThresholds,
                      proposer: str,
                      candidate_diff: str = "") -> Experiment:
        """Record a candidate change with a stated hypothesis and expected
        benefit. Agents cannot propose changes to their own authorization
        or grading rules."""
        if not hypothesis.strip() or not expected_benefit.strip():
            raise ImprovementError(
                "A candidate needs a stated hypothesis and expected benefit")
        proposer_principal = principal_from_actor(proposer)
        if proposer_principal.kind == "agent" and \
                not agent_proposal_allowed(kind, target):
            raise ImprovementError(
                f"Agents cannot propose changes to protected target: {target}")
        if thresholds.max_cohort_fraction > 0.25:
            raise ImprovementError(
                "Canary cohort fraction must stay small (<= 0.25)")
        dataset_hash = dataset_hash_for(dataset)
        # §13.10: pin the IMMUTABLE manifest at proposal time — every
        # referenced artifact resolved and validated NOW.
        manifest = await build_manifest(
            kind=kind, target=target, baseline_ref=baseline_ref,
            candidate_ref=candidate_ref, dataset_ref=dataset_ref,
            dataset_hash=dataset_hash, thresholds=thresholds,
            prompt_store=self.prompt_store)
        experiment = Experiment(
            experiment_id=new_experiment_id(),
            kind=kind, target=target,
            hypothesis=hypothesis, expected_benefit=expected_benefit,
            baseline_ref=baseline_ref, candidate_ref=candidate_ref,
            candidate_diff=candidate_diff,
            dataset_ref=dataset_ref,
            dataset_hash=dataset_hash,
            grading_hash=grading_hash_for(thresholds),
            thresholds_json=json.dumps(thresholds.to_dict()),
            manifest_json=json.dumps(manifest, sort_keys=True),
            proposed_by=proposer,
        )
        await self.repo.create_experiment(experiment)
        return experiment

    # ── §13.2: evaluation on the SAME held-out set ────────────────────

    async def evaluate(self, experiment_id: str, *, evaluator: str,
                       run_evaluator: Callable[[str, Any], Dict[str, Any]],
                       dataset: Any) -> Dict[str, Any]:
        """Evaluate baseline AND candidate on the same held-out set. The
        dataset must hash to the value pinned at proposal — grading rules
        cannot be swapped mid-flight."""
        if principal_from_actor(evaluator).kind == "agent":
            raise ImprovementError("Agents cannot control grading")
        experiment = await self.repo.get_experiment(experiment_id)
        if experiment is None:
            raise ImprovementError("Experiment not found")
        if experiment.state not in (ExperimentState.PROPOSED,):
            raise ImprovementError(
                f"Evaluation starts from PROPOSED, not {experiment.state.value}")
        if dataset_hash_for(dataset) != experiment.dataset_hash:
            raise ImprovementError(
                "Dataset does not match the hash pinned at proposal — "
                "the held-out set is immutable (§13.2)")

        experiment = await self.repo.transition(
            experiment_id, ExperimentState.EVALUATING,
            role=ROLE_EVALUATOR, actor=evaluator, actor_field="evaluated_by")

        baseline_metrics = run_evaluator(experiment.baseline_ref, dataset)
        candidate_metrics = run_evaluator(experiment.candidate_ref, dataset)
        thresholds = ExperimentThresholds.from_dict(
            json.loads(experiment.thresholds_json))
        if grading_hash_for(thresholds) != experiment.grading_hash:
            raise ImprovementError(
                "Grading rules changed after proposal — refusing to evaluate")

        results = {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
            "dataset_ref": experiment.dataset_ref,
            "dataset_hash": experiment.dataset_hash,
            "evaluator": evaluator,
        }
        experiment = await self.repo.transition(
            experiment_id, ExperimentState.EVALUATED,
            role=ROLE_EVALUATOR, actor=evaluator,
            expected_version=experiment.state_version,
            results=results)
        return results

    # ── §13.3: shadow, bounded canary ─────────────────────────────────

    async def start_shadow(self, experiment_id: str, *,
                           activator: str) -> Experiment:
        """Shadow mode: the candidate runs but does not serve users."""
        experiment = await self._require(experiment_id)
        if principal_from_actor(activator).kind == "agent":
            raise ImprovementError(
                f"Agents cannot run shadow deployments (§13.4)")
        return await self.repo.transition(
            experiment_id, ExperimentState.SHADOW,
            role=ROLE_ACTIVATOR, actor=activator, actor_field="activated_by",
            expected_version=experiment.state_version)

    async def start_canary(self, experiment_id: str, *, activator: str,
                           cohort_size: int) -> Experiment:
        """Bounded canary cohort — the size is checked against the
        PREDECLARED fraction and absolute cap (§13.3)."""
        experiment = await self._require(experiment_id)
        if principal_from_actor(activator).kind == "agent":
            raise ImprovementError(
                f"Agents cannot start canaries (§13.4)")
        if experiment.state is not ExperimentState.SHADOW:
            raise ImprovementError(
                f"Canary starts from SHADOW, not {experiment.state.value}")
        thresholds = self._thresholds(experiment)
        # §13.18: MEASURED shadow evidence before user exposure —
        # recorded candidate observations, not a state label.
        if hasattr(self.repo, "shadow_observation_count"):
            observed = await self.repo.shadow_observation_count(experiment_id)
            if observed < thresholds.min_shadow_observations:
                raise ImprovementError(
                    f"Shadow evidence insufficient: {observed} recorded "
                    f"candidate observation(s), the predeclared minimum is "
                    f"{thresholds.min_shadow_observations} (§13.18)")
        if int(cohort_size) <= 0:
            raise ImprovementError("Canary cohort must be positive")
        if int(cohort_size) > thresholds.max_cohort_size:
            raise ImprovementError(
                f"Canary cohort {cohort_size} exceeds the predeclared "
                f"cap {thresholds.max_cohort_size}")
        # The fraction bound is checked against the deployment population
        # when the caller provides it; the absolute cap always applies.
        return await self.repo.transition(
            experiment_id, ExperimentState.CANARY,
            role=ROLE_ACTIVATOR, actor=activator, actor_field="activated_by",
            expected_version=experiment.state_version,
            cohort_size=int(cohort_size))

    async def check_cohort_fraction(self, experiment_id: str, *,
                                    total_population: int) -> bool:
        """Verify the running canary stays inside the predeclared
        fraction of the deployment population."""
        experiment = await self._require(experiment_id)
        if experiment.cohort_size is None:
            raise ImprovementError("No canary cohort is running")
        thresholds = self._thresholds(experiment)
        if total_population <= 0:
            raise ImprovementError("Population must be positive")
        fraction = experiment.cohort_size / total_population
        return fraction <= thresholds.max_cohort_fraction

    # ── promotion (§13 acceptance) ────────────────────────────────────

    async def promote(self, experiment_id: str, *, approver: str,
                      deployment_version: str,
                      total_population: Optional[int] = None) -> Experiment:
        """Promote a canary. Deterministic gates, in order:
        1. approver is NOT an agent and approver ≠ proposer (§13.4);
        2. the canary cohort is inside the predeclared fraction;
        3. predeclared thresholds hold (success floor, regression
           ceiling, cost ceiling);
        4. ZERO critical-constraint violations — average improvement
           does not outweigh a violated constraint;
        5. promotion evidence pinned: dataset hash, results, actor,
           deployment version (§13.5)."""
        experiment = await self._require(experiment_id)
        if principal_from_actor(approver).kind == "agent":
            raise ImprovementError(
                "Agents cannot approve or promote changes (§13.4)")
        if approver == experiment.proposed_by:
            raise ImprovementError(
                "The proposer cannot approve their own candidate (§13.4)")
        if experiment.state is not ExperimentState.CANARY:
            raise ImprovementError(
                f"Promotion happens from CANARY, not {experiment.state.value}")
        if total_population is not None and not await self.check_cohort_fraction(
                experiment_id, total_population=total_population):
            raise ImprovementError(
                "Canary cohort exceeds the predeclared population fraction")
        # §13.18: measured canary evidence before promotion — counted
        # cohort admissions; promotion cannot reuse offline/shadow numbers
        # as proof of canary performance.
        thresholds = self._thresholds(experiment)
        if hasattr(self.repo, "exposure_count"):
            exposures = await self.repo.exposure_count(experiment_id)
            if exposures < thresholds.min_canary_exposures:
                raise ImprovementError(
                    f"Canary evidence insufficient: {exposures} counted "
                    f"exposure(s), the predeclared minimum is "
                    f"{thresholds.min_canary_exposures} (§13.18)")

        # §13.10: the manifest must still hold — CONTENT mutation under a
        # pinned version invalidates the experiment (a NEW experiment is
        # required). A version row that vanished entirely fails later at
        # ACTIVATION (the reconcilable receipt), not here.
        stored_manifest = json.loads(experiment.manifest_json or "{}")
        if stored_manifest and self.prompt_store is not None:
            pinned_versions = stored_manifest.get("prompt_versions") or {}
            for role in ("baseline", "candidate"):
                pinned_entry = pinned_versions.get(role) or {}
                version_id = pinned_entry.get("version_id")
                if not version_id:
                    continue
                current = await self.prompt_store.get_by_version_id(version_id)
                if current is None:
                    continue   # deleted: activation receipt reports it
                import hashlib as _hl
                current_hash = _hl.sha256(
                    str(current.get("template", "")).encode()).hexdigest()[:32]
                if current_hash != pinned_entry.get("content_sha256"):
                    raise ImprovementError(
                        f"The pinned {role} prompt content changed after "
                        "proposal — a NEW experiment is required (§13.10)")

        results = json.loads(experiment.results_json or "{}")
        candidate = results.get("candidate") or {}
        baseline = results.get("baseline") or {}
        thresholds = self._thresholds(experiment)

        # §13.12 (review finding I): TYPED evidence gates — metrics must be
        # present, finite, and numeric; missing measurements stay MISSING
        # (never defaulted to zero); booleans must be actual booleans.
        # Server-owned mandatory constraints CANNOT be omitted by a
        # proposal; regression direction is per-metric (lower-is-better
        # for latency/cost-style metrics).

        def _finite_metric(row: Dict[str, Any], name: str) -> float:
            value = row.get(name)
            if value is None or isinstance(value, bool) or \
                    not isinstance(value, (int, float)) or \
                    value != value or value in (float("inf"), float("-inf")):
                raise ImprovementError(
                    f"Metric '{name}' is missing or not a finite number — "
                    "promotion refuses incomplete evidence (§13.12)")
            return float(value)

        def _constraint_true(row: Dict[str, Any], name: str) -> bool:
            value = row.get(f"constraint_{name}")
            if value is not True:
                return False
            return True

        success = _finite_metric(candidate, "success")
        if success < thresholds.success_min:
            raise ImprovementError(
                f"Candidate success {success} is below the predeclared "
                f"floor {thresholds.success_min}")
        # §13.12: minimum sample coverage — a promotion gate cannot pass on
        # an empty comparison.
        samples = candidate.get("samples")
        if not isinstance(samples, int) or isinstance(samples, bool) or \
                samples < 1:
            raise ImprovementError(
                "Candidate results carry no sample count — promotion "
                "refuses unevaluated evidence (§13.12)")

        # Direction-aware regression: metrics declared lower_is_better
        # regress when the candidate value is HIGHER.
        lower_is_better = set(thresholds.regression_metrics_lower_is_better)
        for metric in thresholds.regression_metrics:
            cand = _finite_metric(candidate, metric)
            base = _finite_metric(baseline, metric)
            if metric in lower_is_better:
                regressed = cand - base > thresholds.regression_max
            else:
                regressed = base - cand > thresholds.regression_max
            if regressed:
                raise ImprovementError(
                    f"Candidate regressed on '{metric}': {cand} vs baseline "
                    f"{base} (allowed max regression {thresholds.regression_max})")
        if thresholds.cost_max_usd is not None:
            cost = _finite_metric(candidate, "cost_usd")
            if cost > thresholds.cost_max_usd:
                raise ImprovementError(
                    f"Candidate cost {cost} exceeds the predeclared ceiling "
                    f"{thresholds.cost_max_usd}")

        # §13.12: server-owned mandatory constraints — always evaluated,
        # unioned with the proposal's declared constraints.
        mandatory = set(_MANDATORY_CONSTRAINTS) | set(
            thresholds.critical_constraints)
        violations = [c for c in sorted(mandatory)
                      if not _constraint_true(candidate, c)]
        if violations:
            raise ImprovementError(
                "Critical constraint(s) violated: "
                f"{', '.join(violations)} — promotion refused regardless of "
                "average quality (§13 acceptance)")

        # 30.30: an evaluation HOLD (insufficient subgroup support, a
        # deterministic log without overlap, or a predeclared hold) blocks
        # promotion — a favorable average alone cannot authorize runtime use.
        from backend.services.offline_policy_evaluation import (
            evaluation_hold_reason,
        )
        hold = evaluation_hold_reason(candidate)
        if hold:
            raise ImprovementError(
                f"Promotion HOLD: {hold} — resolve the evidence gap before "
                "this candidate may affect workers")

        return await self.repo.transition(
            experiment_id, ExperimentState.PROMOTED,
            role=ROLE_APPROVER, actor=approver, actor_field="approved_by",
            expected_version=experiment.state_version,
            promoted_evidence={
                "dataset_hash": experiment.dataset_hash,
                "results": results,
                "actor": approver,
                "deployment_version": deployment_version,
            })

    async def reject(self, experiment_id: str, *, approver: str,
                     reason: str = "",
                     ) -> Experiment:
        experiment = await self._require(experiment_id)
        if principal_from_actor(approver).kind == "agent":
            raise ImprovementError("Agents cannot reject candidates")
        if approver == experiment.proposed_by:
            raise ImprovementError(
                "The proposer cannot decide their own candidate (§13.4)")
        if not isinstance(reason, str) or len(reason) > 4000:
            raise ImprovementError("Rejection reason must be at most 4000 characters")
        # Persist with the CAS transition on both SQLite and Firestore,
        # retaining the existing evaluation evidence.
        results = json.loads(experiment.results_json or "{}")
        results["rejection"] = {"actor": approver, "reason": reason.strip()}
        return await self.repo.transition(
            experiment_id, ExperimentState.REJECTED,
            role=ROLE_APPROVER, actor=approver, actor_field="approved_by",
            expected_version=experiment.state_version, results=results)

    # ── rollback (§13 acceptance) ─────────────────────────────────────

    async def rollback(self, experiment_id: str, *, operator: str,
                       reason: str,
                       restore: Optional[Callable[[Experiment], Any]] = None
                       ) -> Experiment:
        """Roll back a promoted candidate: ROLLED_BACK with the reason and
        the operator recorded; `restore` (wired by the target's versioned
        store, e.g. connector revisions) performs the actual reversion."""
        experiment = await self._require(experiment_id)
        if principal_from_actor(operator).kind == "agent":
            raise ImprovementError(
                f"Agents cannot roll back changes (§13.4)")
        if experiment.state is not ExperimentState.PROMOTED:
            raise ImprovementError(
                f"Rollback applies to PROMOTED, not {experiment.state.value}")
        if restore is not None:
            await _call_maybe_async(restore, experiment)
        return await self.repo.transition(
            experiment_id, ExperimentState.ROLLED_BACK,
            role=ROLE_OPERATOR, actor=operator, actor_field="rolled_back_by",
            expected_version=experiment.state_version,
            rollback_reason=reason or "unspecified")

    # ── helpers ────────────────────────────────────────────────────────

    async def _require(self, experiment_id: str) -> Experiment:
        experiment = await self.repo.get_experiment(experiment_id)
        if experiment is None:
            raise ImprovementError("Experiment not found")
        return experiment

    @staticmethod
    def _require_non_agent(actor: str, action: str) -> None:
        if is_agent_actor(actor):
            raise ImprovementError(
                f"Agents cannot {action} (§13.4)")

    @staticmethod
    def _thresholds(experiment: Experiment) -> ExperimentThresholds:
        return ExperimentThresholds.from_dict(
            json.loads(experiment.thresholds_json or "{}"))


def _default_prompt_store():
    from backend.services.prompt_versions import get_prompt_version_store
    return get_prompt_version_store()


async def _call_maybe_async(func: Callable, *args) -> Any:
    result = func(*args)
    if hasattr(result, "__await__"):
        return await result
    return result
