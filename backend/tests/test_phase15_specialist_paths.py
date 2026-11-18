"""Phase 15 §15.18–§15.21: the complete builder chain, the operationally
useful validator, browser session contracts, and grounded deliverables."""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager
from backend.models.admission import epoch_now




@pytest.fixture(autouse=True)
def _persisted_parent_owner():
    """§27.2 contract: the delegated actor is the parent run's PERSISTED
    owner. These unit fixtures have no workflow repo wired, so the parent
    runs' owners are provided as the fixtures' own tenant (u1) — the same
    fail-closed resolution the production path exercises."""
    from backend.services.agent_authority import set_parent_owner_provider

    async def owner_provider(kind, pid):
        return "u1"

    set_parent_owner_provider(owner_provider)
    yield
    set_parent_owner_provider(None)

# ── §15.18: the builder is one link in the FULL chain ──────────────────

@pytest.mark.asyncio
async def test_generated_never_becomes_available_automatically(tmp_path):
    """The chain: capability request persisted → candidate generated →
    credential binding → operation validation → ACTIVATION GATE → resume.
    'generated' is a CANDIDATE state — availability and execution
    permission remain separately enforced at every link."""
    import uuid as _uuid
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        uid = _uuid.uuid4().hex[:8]
        dispatch = await request_durable_build(
            factory, user_id=f"u-chain-{uid}", method="prompt",
            source=f"Acme API {uid}: invoices", name="acme",
            origin=ORIGIN_ORCHESTRATOR,
            parent_run_id="run-chain", parent_workflow_id="wf-chain",
            parent_task_id="task-chain",
            required_operations=["list_invoices"])
        build = await factory.mcp_repo.get_build(dispatch["build_id"])
        # Link 1: the capability REQUEST persisted (the build row exists
        # in queued/running state — NOT a usable capability).
        assert build["status"] in ("queued", "running", "success",
                                   "pending")
        # Link 2: even with generation 'success', the capability is NOT
        # ready — no MCP record / credentials / validation exist yet.
        readiness = evaluate_capability_readiness(None, build, [])
        assert readiness["ready"] is False
        # Link 3: generation success + registration + credentials still
        # fails the gate without operation validation.
        mcp = {"is_enabled": 1, "active_revision_id": "rev-c",
               "auth_json": '{"credential_name": "acme"}'}
        readiness2 = evaluate_capability_readiness(
            mcp, {**build, "status": "success",
                  "required_operations": ["list_invoices"]}, [])
        assert readiness2["ready"] is False
        assert readiness2["detail"]["missing_operations"] == ["list_invoices"]
    finally:
        await factory.shutdown()


@pytest.mark.asyncio
async def test_activation_gate_releases_the_waiting_parent(tmp_path):
    """The final link: with ALL evidence (operations validated on the
    active revision, bound credential context, live window) the gate
    turns ready — that is what resumes the parked task."""
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    build = {"build_id": "b-chain", "status": "success",
             "mcp_id": "mcp-chain", "credential_reference": "acme",
             "required_operations": ["list_invoices"],
             "parent_task_id": "task-chain"}
    mcp = {"is_enabled": 1, "active_revision_id": "rev-c",
           "auth_json": '{"credential_name": "acme"}'}
    runs = [{"operation_id": "list_invoices", "status": "passed",
             "revision_id": "rev-c", "credential_binding_id": "bind-1",
             "valid_until": epoch_now() + 3600}]
    out = evaluate_capability_readiness(mcp, build, runs)
    assert out["ready"] is True, "the full chain releases the parent"


# ── §15.19: the validator's operational case set + durable records ─────

@pytest.mark.asyncio
async def test_validator_covers_the_full_operational_case_set(tmp_path):
    from backend.agents.connector_validator.validator_agent import (
        ValidationContext, run_validator_via_contract)
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository)
    db = DatabaseManager(str(tmp_path / "val.db"))
    await db.initialize()
    repo = AgentTaskRepository(db)
    ctx = ValidationContext(
        connector_id="mcp-v", operation_id="list_invoices",
        operation_schema={"paginated": True, "writes": False})
    out = await run_validator_via_contract(repo, ctx, parent_id="run-val15",
                                           principal_id="principal-owner")
    kinds = {c["kind"] for c in out["plan"]["test_cases"]}
    assert {"auth_denial", "required_params", "pagination", "empty_response",
            "schema_mismatch", "rate_limit_timeout",
            "credential_revision_change"} <= kinds, \
        "the validator proposes the full operational set"
    await db.close()


@pytest.mark.asyncio
async def test_validation_records_carry_the_exact_scope(tmp_path):
    """Results persist against the EXACT operation, revision, account
    binding, and environment — an unrelated passing operation can never
    validate the capability the workflow requested."""
    db = DatabaseManager(str(tmp_path / "valrec.db"))
    await db.initialize()
    from backend.repositories.sqlite.mcp_repository import (
        SQLiteMCPRepository)
    repo = SQLiteMCPRepository(db)
    await repo.save_validation_run("mcp-scope", {
        "validation_run_id": "vr-1", "revision_id": "rev-1",
        "operation_id": "create_invoice",
        "operation_digest": "d1", "credential_binding_id": "bind-1",
        "authorization_epoch": "e1", "provider_account_id": "acc-1",
        "environment": "production", "runtime_digest": "rt-1",
        "validation_profile_version": "vp-1",
        "case_results_json": json.dumps([{"case": "controlled_write",
                                          "verdict": "pass"}]),
        "started_at": epoch_now(), "completed_at": epoch_now(),
        "valid_until": epoch_now() + 3600,
        "artifact_references_json": "[]", "status": "passed"})
    cursor = await db.execute(
        "SELECT * FROM connector_validation_runs WHERE validation_run_id='vr-1'")
    row = dict(await cursor.fetchone())
    assert row["operation_id"] == "create_invoice"
    assert row["revision_id"] == "rev-1"
    assert row["credential_binding_id"] == "bind-1"
    assert row["environment"] == "production"
    # The readiness gate reads only EXACT-operation evidence.
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    mcp = {"is_enabled": 1, "active_revision_id": "rev-1",
           "auth_json": '{"credential_name": "k"}'}
    build = {"build_id": "b", "status": "success",
             "required_operations": ["list_invoices"]}
    out = evaluate_capability_readiness(mcp, build, [row])
    assert out["ready"] is False
    assert out["detail"]["missing_operations"] == ["list_invoices"], \
        "the passing create_invoice validates NOTHING for list_invoices"
    await db.close()


# ── §15.20: browser session contracts ──────────────────────────────────

def test_browser_contract_is_complete_and_scoped():
    from backend.services.browser_contracts import (
        BrowserContractError, BrowserSessionContract)
    ok = BrowserSessionContract(
        session_id="bs-1", controller_worker_id="w1", controller_epoch="e1",
        allowed_origins=["acme.test"], permitted_actions=["observe",
                                                          "click"],
        deadline_at=epoch_now() + 600, recovery_level="live_continuation",
        run_id="run-b", task_id="t-b",
        expected_postcondition={"url_contains": "/invoices/1"})
    ok.validate()
    with pytest.raises(BrowserContractError, match="unbounded"):
        BrowserSessionContract(
            session_id="bs-2", controller_worker_id="w1",
            controller_epoch="e1", allowed_origins=[],
            permitted_actions=["observe"], deadline_at=1,
            recovery_level="live_continuation", run_id="r", task_id="t",
        ).validate()
    with pytest.raises(BrowserContractError, match="unknown actions"):
        BrowserSessionContract(
            session_id="bs-3", controller_worker_id="w1",
            controller_epoch="e1", allowed_origins=["a.test"],
            permitted_actions=["format_the_disk"], deadline_at=1,
            recovery_level="live_continuation", run_id="r", task_id="t",
        ).validate()


def test_stale_targets_after_handoff_are_refused():
    from backend.services.browser_contracts import (
        BrowserContractError, BrowserSessionContract, check_target_current)
    c = BrowserSessionContract(
        session_id="bs-9", controller_worker_id="w2", controller_epoch="e2",
        allowed_origins=["a.test"], permitted_actions=["click"],
        deadline_at=epoch_now() + 600, recovery_level="live_continuation",
        run_id="r", task_id="t")
    # The page was resolved under the OLD epoch — acting on it now is
    # acting on a stale target.
    with pytest.raises(BrowserContractError, match="stale target"):
        check_target_current(c, resolved_url="https://a.test/x",
                             last_handoff_epoch="e1")
    check_target_current(c, resolved_url="https://a.test/x",
                         last_handoff_epoch="e2")


def test_a_click_is_never_proof_of_completion():
    from backend.services.browser_contracts import (
        BrowserSessionContract, completion_requires_postcondition)
    c = BrowserSessionContract(
        session_id="bs-10", controller_worker_id="w", controller_epoch="e",
        allowed_origins=["a.test"], permitted_actions=["click"],
        deadline_at=epoch_now() + 600, recovery_level="live_continuation",
        run_id="r", task_id="t",
        expected_postcondition={"url_contains": "/confirm"})
    assert completion_requires_postcondition(
        c, clicked=True, postcondition_verified=False) is False, \
        "the click happened; the postcondition did not"
    assert completion_requires_postcondition(
        c, clicked=True, postcondition_verified=True) is True


# ── §15.21: grounded deliverables ──────────────────────────────────────

def test_resume_facts_preserved_and_additions_flagged():
    from backend.services.deliverable_facts import check_resume_facts
    supplied = {"employer": "Acme", "dates": "2020-2024",
                "qualification": "BSc CS"}
    draft = {"employer": "Acme", "dates": "2020-2024",
             "qualification": "BSc CS",
             "achievement": "saved the company $10M"}
    out = check_resume_facts(supplied=supplied, draft=draft)
    assert out["grounded"] is False
    assert out["preserved"] == ["dates", "employer", "qualification"]
    (flag,) = out["unsupported_additions"]
    assert flag["field"] == "achievement"
    assert "not present in the user-supplied facts" in flag["reason"]


def test_report_claims_classified_and_unbacked_flagged():
    """§17.11 (finding E): a SELF-DECLARED source_fact without a verified
    source is UNBACKED — a label cannot vouch for itself. A claim backed
    by a verified fact_id IS a source fact."""
    from backend.services.deliverable_facts import check_report_sources
    out = check_report_sources(
        claims=[{"claim_id": "c1", "kind": "source_fact"},
                {"claim_id": "c2", "kind": "calculation"},
                {"claim_id": "c3", "kind": "uncertain_conclusion"},
                {"claim_id": "c4"},
                {"claim_id": "c5", "kind": "source_fact",
                 "fact_id": "f1"}],
        verified_fact_ids=["f1"])
    # c1 self-declares but has NO fact_id → unbacked (finding E).
    assert out["classification"]["unbacked"] == ["c1", "c4"]
    # c5 carries a VERIFIED fact_id → a real source fact.
    assert out["classification"]["source_fact"] == ["c5"]
    assert out["grounded"] is False
