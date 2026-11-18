"""Phase 27 §27.8 (R4) — the remaining ENABLED specialist roles driven
through their REAL execution paths with the contract lifecycle recorded
(delegate → claim → real deterministic work → typed complete/fail).

The two typed contract runners (outcome_verifier, connector_validator)
are covered by test_phase27_agent_orchestration.py. This file covers
every remaining role whose registry entry routes its work through its
dedicated deterministic service (runner: None) or the recovery worker —
each contract records a real parent/child link, real service output and
an honest disposition. browser_navigator is NOT enabled (its release
stage is off) and stays in the disabled denominator.
"""
import uuid

import pytest
import pytest_asyncio

from backend.models.agent_task import AgentRole, AgentTaskState
from backend.services.agent_contracts import (
    cancel, claim_specialist, complete, delegate, fail,
)
from backend.services.agent_task_contracts import (
    delegate_specialist,
)
from backend.services.specialist_release import (
    ROLE_MIN_STAGE, specialist_stage_flags)


@pytest_asyncio.fixture
async def repo(tmp_path):
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(str(tmp_path / "roles.db"))
    await db.initialize()
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.repositories.routing_repository import (
        RoutingDecisionRepository, set_routing_repo)
    from backend.repositories.sqlite.adaptive_policy_repository import (
        SQLiteAdaptivePolicyRepository, set_adaptive_policy_repo)
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    set_routing_repo(RoutingDecisionRepository(db))
    set_adaptive_policy_repo(SQLiteAdaptivePolicyRepository(db))
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        set_routing_repo(None)
        set_adaptive_policy_repo(None)
        await db.close()


def _enabled_roles():
    flags = specialist_stage_flags()
    enabled_stages = {k for k, v in flags["stages"].items() if v}
    return {role for role, min_stage in ROLE_MIN_STAGE.items()
            if min_stage in enabled_stages}


def _covered_elsewhere():
    return {"outcome_verifier", "connector_validator"}


async def _run_contract(repo, role, run_id, work, input_data,
                        acceptance=None):
    """The COMMON orchestration skeleton: delegate → claim → the role's
    real work executes → typed completion. Returns (contract, done)."""
    contract = await delegate_specialist(
        role, parent_kind="run", parent_id=run_id,
        tenant_id="u1", input_data=input_data,
        acceptance_criteria=acceptance, repo=repo)
    actor = await claim_specialist(repo, contract.contract_id,
                                   role.value)
    out = await work()   # the role's REAL execution path
    done = await complete(contract.contract_id, out["result"],
                          spent_units=out.get("spent", 1), actor=actor,
                          repo=repo)
    return contract, done


# ── §5.10/§5.11: reconciliation + delivery_reconciler ─────────────────

@pytest.mark.asyncio
async def test_reconciliation_roles_resolve_one_uncertain_effect(repo, tmp_path):
    """RECONCILIATION investigates; DELIVERY_RECONCILER resolves the
    ambiguous external delivery. Real effect store, real evidence
    strategy, one conditional resolution — no blind replay."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.effect_repository import (
        SQLiteEffectRepository, request_digest)
    from backend.models.effect import Effect, EffectState, new_effect_id
    from backend.services import reconciliation_service as rs

    db = DatabaseManager(str(tmp_path / "effects-rcn.db"))
    await db.initialize()
    try:
        effect_repo = SQLiteEffectRepository(db)
        effect_id = new_effect_id()
        await effect_repo.prepare_effect(Effect(
            effect_id=effect_id, operation_name="email.send",
            owner_user_id="u1",
            request_digest=request_digest("u1", "email.send",
                                          {"r": ["a@x.io"]}),
            target="a@x.io"))
        await effect_repo.transition(effect_id, EffectState.DISPATCHING,
                                     actor="t")
        await effect_repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN,
                                     actor="t")

        async def evidence(effect):
            return ({"smtp_search": "found", "message_id_match": True},
                    EffectState.CONFIRMED_APPLIED)

        rs.register_evidence_strategy("email.send", evidence)
        try:
            async def work():
                record = await rs.reconcile_effect(effect_repo, effect_id,
                                                   actor="reconciler-u1")
                final = await effect_repo.get_effect(effect_id)
                return {"result": {
                            "effect_id": effect_id,
                            "resolution": final.state.value,
                            "reconciliation_id":
                                record["reconciliation_id"],
                            "resolved": record["resolved"]},
                        "spent": 1}

            contract, done = await _run_contract(
                repo, AgentRole.RECONCILIATION, "run-rcn-1", work,
                {"effect_id": effect_id})
            assert done.state is AgentTaskState.COMPLETED
            assert done.result["effect_id"] == effect_id
            assert done.result["resolution"] == "CONFIRMED_APPLIED"
            assert done.result["resolved"] is True

            # The DELIVERY_RECONCILER inherits the same boundary: a
            # SECOND uncertain delivery resolved the same honest way.
            effect_id2 = new_effect_id()
            await effect_repo.prepare_effect(Effect(
                effect_id=effect_id2, operation_name="email.send",
                owner_user_id="u1",
                request_digest=request_digest("u1", "email.send",
                                              {"r": ["b@x.io"]}),
                target="b@x.io"))
            await effect_repo.transition(effect_id2, EffectState.DISPATCHING,
                                         actor="t")
            await effect_repo.transition(effect_id2,
                                         EffectState.OUTCOME_UNKNOWN,
                                         actor="t")

            async def work2():
                record = await rs.reconcile_effect(effect_repo, effect_id2,
                                                   actor="reconciler-u1")
                final = await effect_repo.get_effect(effect_id2)
                return {"result": {
                            "effect_id": effect_id2,
                            "resolution": final.state.value,
                            "reconciliation_id":
                                record["reconciliation_id"]},
                        "spent": 1}

            contract2, done2 = await _run_contract(
                repo, AgentRole.DELIVERY_RECONCILER, "run-rcn-1", work2,
                {"effect_id": effect_id2})
            assert done2.state is AgentTaskState.COMPLETED
            assert done2.result["resolution"] == "CONFIRMED_APPLIED"
            assert done2.result["effect_id"] != done.result["effect_id"]
        finally:
            rs._STRATEGIES.pop("email.send", None)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconciliation_without_evidence_fails_honestly(repo, tmp_path):
    """A reconciler that finds NO evidence cannot manufacture a
    resolution: the effect stays UNKNOWN and the contract records the
    honest FAILED disposition (never a verified completion)."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.effect_repository import (
        SQLiteEffectRepository, request_digest)
    from backend.models.effect import Effect, EffectState, new_effect_id
    from backend.services import reconciliation_service as rs

    db = DatabaseManager(str(tmp_path / "effects-rcn2.db"))
    await db.initialize()
    try:
        effect_repo = SQLiteEffectRepository(db)
        effect_id = new_effect_id()
        await effect_repo.prepare_effect(Effect(
            effect_id=effect_id, operation_name="email.send",
            owner_user_id="u1",
            request_digest=request_digest("u1", "email.send",
                                          {"r": ["c@x.io"]}),
            target="c@x.io"))
        await effect_repo.transition(effect_id, EffectState.DISPATCHING,
                                     actor="t")
        await effect_repo.transition(effect_id, EffectState.OUTCOME_UNKNOWN,
                                     actor="t")

        async def empty_search(effect):
            return ({"smtp_search": "empty"}, None)   # not proof

        rs.register_evidence_strategy("email.send", empty_search)
        try:
            contract = await delegate_specialist(
                AgentRole.RECONCILIATION, parent_kind="run",
                parent_id="run-rcn-2", tenant_id="u1",
                input_data={"effect_id": effect_id}, repo=repo)
            actor = await claim_specialist(repo, contract.contract_id,
                                           AgentRole.RECONCILIATION.value)
            record = await rs.reconcile_effect(effect_repo, effect_id,
                                               actor="reconciler-u1")
            assert record["resolved"] is False
            final = await effect_repo.get_effect(effect_id)
            assert final.state is EffectState.OUTCOME_UNKNOWN
            failed = await fail(
                contract.contract_id,
                f"no sufficient evidence: {record['reconciliation_id']}",
                actor=actor, repo=repo)
            assert failed.state is AgentTaskState.FAILED
        finally:
            rs._STRATEGIES.pop("email.send", None)
    finally:
        await db.close()


# ── §12.5: recovery_analyst through the REAL worker ───────────────────

@pytest.mark.asyncio
async def test_recovery_analyst_worker_claims_episode_and_completes(
        repo, tmp_path, monkeypatch):
    """RECOVERY_ANALYST's runner is the RecoveryWorker: a real RECOVERING
    task produces a claimed episode and a bounded repair proposal; the
    contract completes with the typed {action} output. The deterministic
    policy gate and episode claim run for real; only the provider leg is
    a controlled stub (the production LLM call boundary)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from backend.models.recovery import RecoveryAction, RecoveryActionEnum
    from backend.models.schemas import (
        Task, TaskRecoveryEvent, TaskStatus, WorkflowRun)
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository, set_recovery_episode_repo)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.repositories.sqlite.workflow_repository import (
        SQLiteWorkflowRepository)
    from backend.workers.recovery_worker import RecoveryWorker

    async def fake_run_adk_debug(agent_factory, prompt, app_name):
        action = RecoveryAction(
            action=RecoveryActionEnum.REPAIR,
            corrected_input={"url": "https://api.example.com/data",
                             "_retry_after": 30},
            rationale="Transient timeout; bounded retry with backoff")
        return [SimpleNamespace(output=action.model_dump_json())]

    from backend.services import gemini_client
    monkeypatch.setattr(gemini_client, "run_adk_debug", fake_run_adk_debug)

    db = DatabaseManager(str(tmp_path / "recovery-roles.db"))
    await db.initialize()
    episode_repo = RecoveryEpisodeRepository(db)
    set_recovery_episode_repo(episode_repo)
    try:
        wf_repo = SQLiteWorkflowRepository(db)
        bus = MagicMock()
        bus.publish = AsyncMock()
        engine = SimpleNamespace(evaluate_dag=AsyncMock(),
                                 notification_repo=None)
        worker = RecoveryWorker(bus, wf_repo, engine)
        task = Task(
            task_id="t-rc", run_id="run-rc", workflow_id="wf-rc",
            user_id="u1", agent="core.http",
            input_data={"url": "https://api.example.com/data"},
            status=TaskStatus.RECOVERING, error="timeout",
            error_type="TIMEOUT_ERROR", recovery_attempts=0,
            recovery_history=[])
        run = WorkflowRun(run_id="run-rc", workflow_id="wf-rc",
                          user_id="u1", goal="test", status="RUNNING",
                          tasks=[task])
        await wf_repo.save_run(run.model_dump(mode="json"))
        event = TaskRecoveryEvent(run_id="run-rc", workflow_id="wf-rc",
                                  task_id="t-rc", recovery_attempt=1)

        async def work():
            await worker.handle_recovery_event(
                event.model_dump(mode="json"), None)
            episode = await episode_repo.get_episode_for_task(
                "run-rc", "t-rc")
            assert episode, "the worker must create the claim episode"
            loaded = await wf_repo.get_run("run-rc")
            final_task = next(t for t in loaded["tasks"]
                              if t["task_id"] == "t-rc")
            assert str(final_task.get("status", "")).lower() == "pending", (
                f"repair must requeue the task, got {final_task.get('status')}")
            return {"result": {"action": str(final_task.get("status", "")),
                               "episode_id": episode["episode_id"],
                               "failure_code":
                                   episode["failure_code"]},
                    "spent": 2}

        contract, done = await _run_contract(
            repo, AgentRole.RECOVERY_ANALYST, "run-rc", work,
            {"run_id": "run-rc", "task_id": "t-rc"})
        assert done.state is AgentTaskState.COMPLETED
        assert done.result["action"].upper() == "PENDING"
        assert done.result["episode_id"]
    finally:
        set_recovery_episode_repo(None)
        await db.close()


# ── §13.1: improvement_proposer through the REAL pipeline ─────────────

@pytest.mark.asyncio
async def test_improvement_proposer_proposes_from_reviewed_clusters(
        repo, tmp_path):
    """IMPROVEMENT_PROPOSER turns REAL reviewed failure episodes into a
    versioned candidate through the rollout pipeline; the typed output
    carries the hypothesis and the created experiment."""
    from backend.repositories.recovery_episode_repository import RecoveryState
    from backend.repositories.experiment_repository import (
        ExperimentRepository, set_experiment_repo)
    from backend.repositories.recovery_episode_repository import (
        RecoveryEpisodeRepository)
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.improvement_rollout import (
        propose_from_failure_clusters)
    from backend.services.improvement_service import ImprovementService
    from backend.services.prompt_versions import (
        PolicySnapshotStore, PromptVersionStore,
        set_policy_snapshot_store, set_prompt_version_store)

    BASELINE = "You are a careful read-only research assistant. {goal}"
    CANDIDATE = ("You are a careful read-only research assistant. {goal}\n"
                 "GUARDRAIL: never repeat a failed request without "
                 "changed arguments.")

    db = DatabaseManager(str(tmp_path / "rollout-roles.db"))
    await db.initialize()
    store = PromptVersionStore(db)
    snapshots = PolicySnapshotStore(db)
    exp_repo = ExperimentRepository(db)
    set_experiment_repo(exp_repo)
    set_prompt_version_store(store)
    set_policy_snapshot_store(snapshots)
    try:
        episodes_repo = RecoveryEpisodeRepository(db)
        marker = uuid.uuid4().hex[:8]
        for i in range(2):
            ep = await episodes_repo.create_episode({
                "episode_id": f"ep-{marker}-{i}",
                "tenant_id": "u1", "actor_id": "system",
                "run_id": f"run-{marker}", "task_id": f"t-{marker}-{i}",
                "failure_code": "TIMEOUT_STREAK",
                "goal_digest": uuid.uuid4().hex[:16],
                "acceptance_criteria": [],
                "deadline_at": 0})
            await episodes_repo.claim_episode(
                ep["episode_id"], "worker-p27", lease_seconds=120)
            await episodes_repo.transition(
                ep["episode_id"], RecoveryState.ESCALATED,
                actor="system", error_code="TIMEOUT_STREAK")

        baseline_v = await store.checkout(
            "read_only_task_instructions", BASELINE, created_by="operator:a")
        await store.activate("read_only_task_instructions",
                             baseline_v["version_id"],
                             activated_by="operator:a")

        def draft(cluster):
            return CANDIDATE

        async def work():
            proposed = await propose_from_failure_clusters(
                episodes_repo, ImprovementService(exp_repo), store,
                dataset={"cases": [1]}, draft_candidate=draft)
            assert proposed, "reviewed episodes with a drafter propose"
            first = proposed[0]
            return {"result": {"hypothesis": CANDIDATE,
                               "experiment_id": first["experiment_id"]},
                    "spent": 1}

        contract, done = await _run_contract(
            repo, AgentRole.IMPROVEMENT_PROPOSER, "run-prop-1", work,
            {"failure_code": "TIMEOUT_STREAK"})
        assert done.state is AgentTaskState.COMPLETED
        assert done.result["hypothesis"]
        assert done.result["experiment_id"]
    finally:
        set_experiment_repo(None)
        set_prompt_version_store(None)
        set_policy_snapshot_store(None)
        await db.close()


# ── §17: deliverable_assembler / provenance / document quality ────────

@pytest.mark.asyncio
async def test_deliverable_assembler_builds_and_records_artifact(repo, tmp_path):
    """DELIVERABLE_ASSEMBLER: the artifact builder pipeline executes and
    the contract records the typed {artifact_id, manifest} output."""
    from backend.services.artifact_builder import save_user_files

    body = ("<html><body><h1>Phase 27 assembly</h1>"
            "<p>Typed deliverable body.</p></body></html>")

    async def work():
        out = save_user_files("u1", "phase27-assembly",
                              [{"path": "index.html", "content": body}])
        return {"result": {"artifact_id": out["artifact_id"],
                           "manifest": {"files": out.get("files",
                                                        ["index.html"]),
                                        "bytes": len(body)}},
                "spent": 1}

    contract, done = await _run_contract(
        repo, AgentRole.DELIVERABLE_ASSEMBLER, "run-deliv-1", work,
        {"kind": "html", "name": "phase27-assembly"})
    assert done.state is AgentTaskState.COMPLETED
    assert done.result["artifact_id"]
    assert done.result["manifest"]["files"]


@pytest.mark.asyncio
async def test_provenance_reviewer_flags_unsupported_claims(repo):
    """PROVENANCE_REVIEWER: the deterministic fact check runs and the
    contract records {unsupported_claims, conflicts} — inventions are
    flagged, grounded values are preserved."""
    from backend.services.deliverable_facts import check_resume_facts

    supplied = {"employer": "Example Corp",
                "degree": "BSc Computer Science"}
    draft = {"employer": "Example Corp",
             "degree": "PhD",   # changed value under the same key
             "employer_2": "Nonexistent Industries"}

    async def work():
        verdict = check_resume_facts(supplied=supplied, draft=draft)
        unsupported = [f for f in verdict["unsupported_additions"]
                       if "CONTRADICTION" not in f.get("reason", "")]
        conflicts = [f for f in verdict["unsupported_additions"]
                     if "CONTRADICTION" in f.get("reason", "")]
        return {"result": {"unsupported_claims": unsupported,
                           "conflicts": conflicts,
                           "grounded": verdict["grounded"]},
                "spent": 1}

    contract, done = await _run_contract(
        repo, AgentRole.PROVENANCE_REVIEWER, "run-prov-1", work,
        {"fields": sorted(draft)})
    assert done.state is AgentTaskState.COMPLETED
    assert any(f["value"] == "Nonexistent Industries"
               for f in done.result["unsupported_claims"])
    assert any(f["value"] == "PhD" for f in done.result["conflicts"])
    assert done.result["grounded"] is False


@pytest.mark.asyncio
async def test_document_quality_reviewer_records_layout_issues(repo):
    """DOCUMENT_QUALITY_REVIEWER: the rendered verifier runs on real
    HTML; the typed output lists layout/readability issues and cannot
    override a deterministic failure."""
    from backend.services.rendered_document_verifier import (
        verify_rendered_document)

    async def work():
        verdict = await verify_rendered_document(
            html="<html><body><h1>Report</h1>"
                 "<p>Readable paragraph text.</p></body></html>",
            expected_text=["Report", "Readable paragraph text."])
        blocking = [c["check"] for c in verdict.get("checks", [])
                    if c.get("status") != "pass"]
        return {"result": {"layout_issues": blocking,
                           "verdict": verdict.get("verdict")},
                "spent": 1}

    contract, done = await _run_contract(
        repo, AgentRole.DOCUMENT_QUALITY_REVIEWER, "run-docq-1", work,
        {"sections": ["Report"]})
    assert done.state is AgentTaskState.COMPLETED
    assert done.result["layout_issues"] == []


# ── §12/§22: evaluation_curator / release_verifier ────────────────────

@pytest.mark.asyncio
async def test_evaluation_curator_proposes_cases_but_cannot_edit_heldout(
        repo, tmp_path):
    """EVALUATION_CURATOR: real dataset service — proposed cases are
    recorded as a draft set; the held-out set is draft-proof (editing
    requires operator approval authority)."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.evaluation_datasets import EvaluationDatasetService

    db = DatabaseManager(str(tmp_path / "eval-roles.db"))
    await db.initialize()
    try:
        service = EvaluationDatasetService(db)
        cases = [{"family": "existing_tool", "input": {"goal": "g"},
                  "expect": {"contains": "ok"}}]

        async def work():
            ds = await service.create_dataset(
                name="curated-" + uuid.uuid4().hex[:6],
                set_kind="development",
                cases=cases, created_by="agent:eval-curator",
                provenance="proposed by evaluation_curator contract")
            return {"result": {"proposed_cases": ds["cases"],
                               "dataset_id": ds["dataset_id"]},
                    "spent": 1}

        contract, done = await _run_contract(
            repo, AgentRole.EVALUATION_CURATOR, "run-curate-1", work,
            {"family": "existing_tool"})
        assert done.state is AgentTaskState.COMPLETED
        assert done.result["proposed_cases"] == cases

        # The boundary: the same agent principal cannot edit a HELD-OUT
        # set — approval authority is required (§13.11).
        heldout_cases = [
            {"family": fam, "input": {"goal": "g"}, "expect": {"contains": "ok"}}
            for fam in ("supported_success", "unsupported_task",
                        "missing_credentials", "ambiguous_outcome",
                        "malformed_output", "factuality_failure")]
        heldout = await service.create_dataset(
            name="heldout-" + uuid.uuid4().hex[:6], set_kind="heldout",
            cases=heldout_cases, created_by="operator:a")
        from backend.services.evaluation_datasets import DatasetError
        with pytest.raises(DatasetError) as denied:
            await service.update_cases(
                heldout["dataset_id"], cases=heldout_cases,
                actor="agent:eval-curator")
        assert "draft-proof" in str(denied.value).lower() or \
            "approval" in str(denied.value).lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_release_verifier_assembles_real_evidence(repo, tmp_path):
    """RELEASE_VERIFIER: assembles candidate evidence through the REAL
    evidence register — counts by level plus the blocking rows. The
    role cannot self-certify: recording evidence is all it does."""
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.services.evidence_register import EvidenceRegister

    db = DatabaseManager(str(tmp_path / "evidence-roles.db"))
    await db.initialize()
    try:
        register = EvidenceRegister(db)
        await register.record(subphase_id="27.16", claim="hosted providers",
                              status="blocked",
                              test_ref="test_phase25_cloud_providers",
                              evidence_level="blocked",
                              recorded_by="operator:a")
        await register.record(subphase_id="27.1", claim="qualification gate",
                              status="pass",
                              test_ref="test_phase27_stage_schemas",
                              evidence_level="verified_locally",
                              recorded_by="operator:a")

        async def work():
            summary = await register.summary()
            return {"result": {"checks": [
                                   {"subphase": "27.16", "ok": False},
                                   {"subphase": "27.1", "ok": True}],
                               "summary": summary},
                    "spent": 1}

        contract, done = await _run_contract(
            repo, AgentRole.RELEASE_VERIFIER, "run-relv-1", work,
            {"register": "evidence_register"})
        assert done.state is AgentTaskState.COMPLETED
        assert done.result["summary"]["total"] == 2
        assert done.result["summary"]["blocking"] == ["27.16"]
    finally:
        await db.close()


# ── §17.17: build_verifier interprets REAL build evidence ─────────────

@pytest.mark.asyncio
async def test_build_verifier_reports_honest_build_state(repo):
    """BUILD_VERIFIER: the project execution record is the real
    build-evidence producer — without an executed runner the state is
    honestly unexecuted and no badge may be awarded."""
    from backend.services.generated_project_states import (
        award_badges, project_execution_record)

    async def work():
        record = project_execution_record(
            source_digest=uuid.uuid4().hex,
            dependency_lock_digest=uuid.uuid4().hex,
            declared_test_scope="unit")
        badges = award_badges(record)
        return {"result": {"build_state": record["state"],
                           "evidence": {"source_digest":
                                        record["source_digest"],
                                        "badges": badges,
                                        "note": record["note"]}},
                "spent": 1}

    contract, done = await _run_contract(
        repo, AgentRole.BUILD_VERIFIER, "run-bv-1", work,
        {"runner": "none"})
    assert done.state is AgentTaskState.COMPLETED
    assert done.result["build_state"] == "build_not_executed"
    assert done.result["evidence"]["badges"] == []


# ── §9.4: dom_resolver resolves REAL observations ─────────────────────

@pytest.mark.asyncio
async def test_dom_resolver_resolves_and_refuses_ambiguity(repo):
    """DOM_RESOLVER: the semantic target resolver runs on a REAL
    BrowserObservation — a unique target resolves; an ambiguous one is
    REFUSED, never silently picked. The role has no registry entry, so
    the delegation uses the plain contract path with its scoped tool."""
    from backend.models.browser_observation import (
        BrowserObservation, ObservedElement)
    from backend.services.browser_target_resolver import resolve_target

    def observation(with_dup=False):
        elements = [
            ObservedElement(target_reference="e1", semantic_role="button",
                            accessible_name="Delete",
                            semantic_container="row INV-1042"),
            ObservedElement(target_reference="e2", semantic_role="textbox",
                            accessible_name="Invoice number",
                            semantic_container="row INV-1042")]
        if with_dup:
            elements.append(ObservedElement(
                target_reference="e3", semantic_role="button",
                accessible_name="Delete",
                semantic_container="row INV-1042"))
        return BrowserObservation(
            observation_id="obs-" + uuid.uuid4().hex[:8],
            session_id="sess-dom", owner_generation=1,
            controller_generation=1, page_id="pg-1",
            sanitized_url="https://app.example.test/invoices",
            origin="https://app.example.test",
            page_title="Invoices", elements=elements)

    async def work_unique():
        target, failure, detail = resolve_target(
            observation(), target_ref="e1")
        assert target is not None and failure is None, detail
        return {"result": {"resolved_ref": target.element.target_reference,
                           "strategy": target.strategy,
                           "ambiguous": False},
                "spent": 1}

    contract = await delegate(
        parent_kind="run", parent_id="run-dom-1",
        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
        input_data={"page": "invoices"},
        allowed_tools=["browser.observe"], goal="resolve Delete button",
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a resolved target exists"}],
        repo=repo)
    actor = await claim_specialist(repo, contract.contract_id,
                                   AgentRole.DOM_RESOLVER.value)
    out = await work_unique()
    done = await complete(contract.contract_id, out["result"],
                          spent_units=1, actor=actor, repo=repo)
    assert done.state is AgentTaskState.COMPLETED
    assert done.result["resolved_ref"] == "e1"

    # The ambiguity refusal: two identical candidates in one container
    # cannot be silently narrowed to .first — the contract records the
    # honest FAILED disposition.
    contract2 = await delegate(
        parent_kind="run", parent_id="run-dom-1",
        role=AgentRole.DOM_RESOLVER, tenant_id="u1",
        input_data={"page": "invoices-dup"},
        allowed_tools=["browser.observe"],
        goal="resolve ambiguous Delete",
        acceptance_criteria=[{"kind": "non_empty",
                              "description": "a resolution or refusal exists"}],
        repo=repo)
    actor2 = await claim_specialist(repo, contract2.contract_id,
                                    AgentRole.DOM_RESOLVER.value)
    target, failure, detail = resolve_target(
        observation(with_dup=True), role="button", accessible_name="Delete")
    assert target is None
    assert failure is not None and "AMBIGUOUS" in failure.value
    failed = await fail(contract2.contract_id,
                        f"ambiguous target refused by §9.4: {detail}",
                        actor=actor2, repo=repo)
    assert failed.state is AgentTaskState.FAILED


# ── §15.9: cancellation of a service-path contract ────────────────────

@pytest.mark.asyncio
async def test_service_role_contract_cancels_with_honest_disposition(repo):
    """A delegated service-path role can be cancelled before work
    starts; the parent sees CANCELLED, not a fake completion."""
    contract = await delegate_specialist(
        AgentRole.EVALUATION_CURATOR, parent_kind="run",
        parent_id="run-cancel-1", tenant_id="u1",
        input_data={"family": "existing_tool"}, repo=repo)
    out = await cancel(contract.contract_id, actor="operator",
                       repo=repo)
    state = out.get("state")
    if hasattr(state, "value"):
        state = state.value
    assert state == "CANCELLED"


# ── the coverage assertion itself ─────────────────────────────────────

def test_every_enabled_role_is_covered_by_real_journeys():
    """27.R4 closure: enabled inventory minus the typed-runner roles
    covered in test_phase27_agent_orchestration.py must all appear in
    THIS file's real journeys."""
    enabled = _enabled_roles()
    covered_here = {
        "reconciliation", "delivery_reconciler", "recovery_analyst",
        "improvement_proposer", "deliverable_assembler",
        "provenance_reviewer", "document_quality_reviewer",
        "evaluation_curator", "release_verifier", "build_verifier",
        "dom_resolver"}
    remaining = enabled - covered_here - _covered_elsewhere()
    assert not remaining, f"enabled roles without journeys: {remaining}"
    # and the enabled inventory stays frozen
    assert enabled == {
        "deliverable_assembler", "provenance_reviewer",
        "document_quality_reviewer", "build_verifier",
        "delivery_reconciler", "outcome_verifier", "dom_resolver",
        "evaluation_curator", "release_verifier", "improvement_proposer",
        "reconciliation", "recovery_analyst", "connector_validator"}
