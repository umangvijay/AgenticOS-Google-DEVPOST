"""Phase 15 acceptance tests (roadmap-named files): the verifier and
validator agents run through bounded contracts with typed outputs, the
specialist registry resolves every role, and the §12.19 combination rule
holds at the contract boundary."""
import pytest
import pytest_asyncio

from backend.models.agent_task import AgentRole, AgentTaskError
from backend.repositories.agent_task_repository import (
    AgentTaskRepository, set_agent_task_repo)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.agent_task_contracts import (
    SPECIALIST_REGISTRY, delegate_specialist, resolve_runner)


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "specialists-p15.db"))
    await db.initialize()
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


# ── The registry covers the roadmap's specialist table ────────────────

def test_specialist_registry_covers_the_roadmap_roles():
    roles = {r.value for r in AgentRole}
    assert {"outcome_verifier", "connector_validator", "recovery_analyst",
            "reconciliation", "evaluation_curator", "improvement_proposer",
            "release_verifier"} <= roles
    for role, entry in SPECIALIST_REGISTRY.items():
        assert entry["boundary"], f"{role} declares its authority boundary"
        assert entry["output_schema"], f"{role} declares its output schema"
    # The two roadmap-named runners resolve.
    assert "run_verifier_via_contract" in resolve_runner(
        AgentRole.OUTCOME_VERIFIER)
    assert "run_validator_via_contract" in resolve_runner(
        AgentRole.CONNECTOR_VALIDATOR)


# ── The verifier agent through its contract ────────────────────────────

@pytest.mark.asyncio
async def test_verifier_contract_deterministic_fallback(repo):
    """§15.1: without a model client the verifier completes HONESTLY with
    the deterministic verdict (never fabricated model output), and the
    §12.19 combination rule holds at the contract boundary."""
    from backend.agents.verifier.verifier_agent import (
        VERIFIER_OUTPUT_SCHEMA, VerificationContext,
        run_verifier_via_contract)
    ctx = VerificationContext(
        task_id="t1", run_id="run-v", goal="g",
        criteria=[{"kind": "contains", "value": "invoice"}],
        output_data={"reply": "here is the invoice"},
        deterministic_verdict="pass", deterministic_reasons=[])
    out = await run_verifier_via_contract(repo, ctx, parent_id="run-v",
                                          principal_id="owner-u1")
    assert out["combined_verdict"] == "pass"
    assert out["result"]["reasoning"].startswith("deterministic verdict")
    done = await repo.get_contract(out["contract_id"])
    assert done.state.value == "COMPLETED"


@pytest.mark.asyncio
async def test_verifier_contract_fail_stands_over_judge_pass(repo):
    """§12.19 at the contract boundary: a deterministic FAIL is not
    upgraded even if a judge model claims pass."""
    from backend.agents.verifier.verifier_agent import (
        VERIFIER_OUTPUT_SCHEMA, VerificationContext,
        run_verifier_via_contract)

    async def optimistic_judge(prompt):
        class _E:
            output = {"verdict": "pass", "criterion_results": [],
                      "missing_evidence": [], "reasoning": "looks great"}
        return [_E()]

    ctx = VerificationContext(
        task_id="t2", run_id="run-v2", goal="g",
        criteria=[{"kind": "contains", "value": "invoice"}],
        output_data={"reply": "no invoice here"},
        deterministic_verdict="fail",
        deterministic_reasons=["output does not mention 'invoice'"])
    out = await run_verifier_via_contract(repo, ctx, model="judge-model",
                                          principal_id="owner-u1",
                                          judge_client=optimistic_judge,
                                          parent_id="run-v2")
    assert out["combined_verdict"] == "fail", \
        "the judge's pass NEVER upgrades a deterministic fail"


@pytest.mark.asyncio
async def test_verifier_dedup_per_task(repo):
    """§15.4: duplicate verification assignments dedup by (run, task)."""
    from backend.agents.verifier.verifier_agent import (
        VERIFIER_OUTPUT_SCHEMA, VerificationContext,
        run_verifier_via_contract)
    ctx = VerificationContext(
        task_id="t3", run_id="run-v3", goal="g", criteria=[],
        output_data={"reply": "ok"}, deterministic_verdict="pass",
        deterministic_reasons=[])
    # §15.4: dedup prevents CONCURRENT duplicate assignment; sequential
    # re-verification after COMPLETION is a new logical assignment (the
    # dedup index only covers active contracts).
    first = await run_verifier_via_contract(repo, ctx, parent_id="run-v3",
                                            principal_id="owner-u1")
    second = await run_verifier_via_contract(repo, ctx, parent_id="run-v3",
                                             principal_id="owner-u1")
    assert second["contract_id"] != first["contract_id"]
    # CONCURRENT duplicate CREATES: exactly one wins (deterministic — the
    # race is at the create, which is what the dedup index protects; the
    # full lifecycle racing is scheduling-dependent).
    import asyncio
    from backend.services.agent_contracts import delegate
    _criteria = [{"kind": "non_empty",
                  "description": "a structured verdict exists"}]
    results = await asyncio.gather(
        delegate(parent_kind="run", parent_id="run-v5",
                 role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                 input_data=ctx.model_dump(),
                 output_schema=VERIFIER_OUTPUT_SCHEMA,
                 allowed_tools=["evidence.read", "criteria.verify"],
                 acceptance_criteria=_criteria,
                 dedup_key=f"verify:{ctx.run_id}:{ctx.task_id}",
                 repo=repo),
        delegate(parent_kind="run", parent_id="run-v5",
                 role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1",
                 input_data=ctx.model_dump(),
                 output_schema=VERIFIER_OUTPUT_SCHEMA,
                 allowed_tools=["evidence.read", "criteria.verify"],
                 acceptance_criteria=_criteria,
                 dedup_key=f"verify:{ctx.run_id}:{ctx.task_id}",
                 repo=repo),
        return_exceptions=True)
    errs = [r for r in results if isinstance(r, AgentTaskError)]
    contracts = [r for r in results if not isinstance(r, AgentTaskError)]
    assert len(errs) == 1 and len(contracts) == 1, \
        "exactly one duplicate create wins"
    assert "Duplicate" in str(errs[0])


# ── The validator agent through its contract ───────────────────────────

@pytest.mark.asyncio
async def test_validator_contract_baseline_cases(repo):
    """§15.2: without a model the validator proposes the DETERMINISTIC
    baseline case set (auth denial, required params, malformed response);
    writes/pagination extend it by schema."""
    from backend.agents.connector_validator.validator_agent import (
        ValidationContext, run_validator_via_contract)
    ctx = ValidationContext(
        connector_id="mcp-1", operation_id="create_draft",
        operation_schema={"writes": True},
        existing_coverage=[])
    out = await run_validator_via_contract(repo, ctx, parent_id="run-val",
                                           principal_id="owner-u1")
    cases = out["plan"]["test_cases"]
    kinds = {c["kind"] for c in cases}
    assert {"auth_denial", "required_params", "malformed_response",
            "controlled_write"} <= kinds
    assert out["plan"]["operation_id"] == "create_draft"
    done = await repo.get_contract(out["contract_id"])
    assert done.state.value == "COMPLETED"
    assert done.result["operation_id"] == "create_draft"


@pytest.mark.asyncio
async def test_validator_model_proposal_recorded(repo):
    """§15.2: with a model proposer, the proposal is RECORDED as the
    contract's typed output — execution still goes through the §6
    pipeline (the agent cannot invent passing results)."""
    from backend.agents.connector_validator.validator_agent import (
        ValidationContext, run_validator_via_contract)

    async def model_proposer(prompt):
        class _E:
            output = {"operation_id": "list_invoices",
                      "test_cases": [{"name": "model_case", "kind":
                                      "pagination",
                                      "request_hint": "page size 1",
                                      "expected_observation": "stable"}],
                      "risks": ["rate limits"]}
        return [_E()]

    ctx = ValidationContext(connector_id="mcp-2",
                            operation_id="list_invoices",
                            operation_schema={"paginated": True})
    out = await run_validator_via_contract(repo, ctx, model="m",
                                           principal_id="owner-u1",
                                           judge_client=model_proposer,
                                           parent_id="run-val2")
    assert out["plan"]["test_cases"][0]["name"] == "model_case"
    assert "rate limits" in out["plan"]["risks"][0]


# ── delegate_specialist: registry-driven delegation ────────────────────

@pytest.mark.asyncio
async def test_delegate_specialist_uses_registry_schema(repo):
    contract = await delegate_specialist(
        AgentRole.RELEASE_VERIFIER, parent_kind="run", parent_id="run-rv",
        tenant_id="u1", input_data={"candidate": "v1"}, repo=repo)
    assert contract.output_schema == {"required": ["checks", "summary"]}
    assert contract.role is AgentRole.RELEASE_VERIFIER


@pytest.mark.asyncio
async def test_delegate_specialist_unknown_role_refused(repo):
    """A role outside the specialist registry refuses — the browser
    navigator executes through the browser runtime, not this delegation
    path."""
    from backend.models.agent_task import AgentTaskError
    assert AgentRole.BROWSER_NAVIGATOR not in SPECIALIST_REGISTRY
    with pytest.raises(AgentTaskError, match="Unknown specialist role"):
        await delegate_specialist(
            AgentRole.BROWSER_NAVIGATOR, parent_kind="run",
            parent_id="run-x", tenant_id="u1", input_data={}, repo=repo)
