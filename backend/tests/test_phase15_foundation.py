"""Phase 15 §15.0–§15.5: the delegation baseline — inventory, the
deterministic delegation policy, hierarchy reconstruction, the complete
contract, authority intersection, and the authorization recheck."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager




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

@pytest_asyncio.fixture
async def repo(tmp_path):
    db = DatabaseManager(str(tmp_path / "p15a.db"))
    await db.initialize()
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    r = AgentTaskRepository(db)
    set_agent_task_repo(r)
    try:
        yield r
    finally:
        set_agent_task_repo(None)
        await db.close()


# ── §15.0: the inventory — every advertised role has a real path ───────

def test_every_advertised_specialist_has_an_execution_path():
    from backend.services.delegation_policy import specialist_inventory
    inv = specialist_inventory()
    assert inv, "the inventory lists the specialists"
    for entry in inv:
        assert entry["callable"] or entry["placeholder"], \
            f"{entry['role']} is neither callable nor marked placeholder"
        assert entry["authority_boundary"], "each role declares its boundary"
        assert entry["output_schema"], "each role declares its output"


def test_placeholder_roles_are_marked_not_advertised_as_callable():
    """A role with NO execution path is explicitly a placeholder — the
    inventory cannot advertise a disconnected helper as production."""
    from backend.services.delegation_policy import (
        _SERVICE_EXECUTED, specialist_inventory)
    from backend.models.agent_task import AgentRole
    by_role = {e["role"]: e for e in specialist_inventory()}
    # The runner-backed roles are callable through their runner.
    assert by_role["outcome_verifier"]["runner"]
    # Service-executed roles name their service path.
    for role in _SERVICE_EXECUTED:
        assert by_role[role.value]["callable"]


# ── §15.1: the deterministic delegation decision ───────────────────────

def test_simple_tasks_stay_simple():
    from backend.services.delegation_policy import should_delegate
    out = should_delegate(simple_lookup=True, read_only=True)
    assert out.delegate is False
    assert "inline" in out.reason
    assert "inline_execution" in out.alternatives_considered


def test_verification_needs_delegate_to_the_verifier():
    from backend.services.delegation_policy import should_delegate
    out = should_delegate(requires_verification=True)
    assert out.delegate is True
    assert "cannot grade its own output" in out.reason
    assert out.suggested_role is not None


def test_distinct_capability_and_parallel_work_delegate():
    from backend.services.delegation_policy import should_delegate
    cap = should_delegate(requires_distinct_capability=True)
    assert cap.delegate and "distinct capability" in cap.reason
    par = should_delegate(parallelizable=True)
    assert par.delegate and par.parallelizable


# ── §15.2: one authoritative hierarchy, reconstructable ────────────────

@pytest.mark.asyncio
async def test_hierarchy_reconstructs_from_persistence_after_restart(repo):
    from backend.services.agent_contracts import delegate
    from backend.models.agent_task import AgentRole
    root = await delegate(
        parent_kind="run", parent_id="run-h", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", goal="verify the run",
        input_data={"task_id": "t1"},
        output_schema={"required": ["verdict"]},
        acceptance_criteria=[{"kind": "non_empty"}],
        allowed_tools=["evidence.read"], repo=repo)
    mid = await delegate(
        parent_kind="contract", parent_id=root.contract_id,
        role=__import__("backend.models.agent_task",
                        fromlist=["AgentRole"]).AgentRole.RECOVERY_ANALYST,
        tenant_id="u1", goal="analyze failure",
        input_data={"x": 1}, output_schema={"required": ["action"]},
        acceptance_criteria=[{"kind": "non_empty"}],
        allowed_tools=["evidence.read"], depth=1,
        ancestor_ids=[root.contract_id], repo=repo)
    # "Restart": a fresh read of the PERSISTED tree.
    tree = await repo.subtree_of(root.contract_id)
    ids = [c.contract_id for c in tree]
    assert mid.contract_id in ids
    assert root.contract_id not in ids, "the root is not its own child"
    assert len(ids) == 1


# ── §15.3: the complete contract — no ambiguous start ──────────────────

@pytest.mark.asyncio
async def test_delegation_refuses_incomplete_contracts(repo):
    from backend.services.agent_contracts import delegate
    from backend.models.agent_task import AgentRole, AgentTaskError
    with pytest.raises(AgentTaskError, match="acceptance_criteria"):
        await delegate(
            parent_kind="run", parent_id="run-i1", role=AgentRole.OUTCOME_VERIFIER,
            tenant_id="u1", goal="g", input_data={"x": 1},
            output_schema={"required": ["verdict"]},
            allowed_tools=["evidence.read"], repo=repo)   # no criteria
    with pytest.raises(AgentTaskError, match="allowed_tools"):
        await delegate(
            parent_kind="run", parent_id="run-i2", role=AgentRole.OUTCOME_VERIFIER,
            tenant_id="u1", goal="g", input_data={"x": 1},
            output_schema={"required": ["verdict"]},
            acceptance_criteria=[{"kind": "non_empty"}],
            allowed_tools=[], repo=repo)


@pytest.mark.asyncio
async def test_contract_carries_the_full_field_set(repo):
    from backend.services.agent_contracts import delegate
    from backend.models.agent_task import AgentRole
    c = await delegate(
        parent_kind="run", parent_id="run-f", role=AgentRole.RECOVERY_ANALYST,
        tenant_id="u1", goal="repair the connector call",
        exclusions=["do not expand the goal"],
        input_refs=["artifact:ev-1"], required_capabilities=["mcp.exec"],
        input_data={"x": 1}, output_schema={"required": ["action"]},
        acceptance_criteria=[{"kind": "non_empty"}],
        allowed_tools=["evidence.read"], budget_units=50,
        agent_version="v1", tool_version="t1", policy_version="p1",
        repo=repo)
    fresh = await repo.get_contract(c.contract_id)
    assert fresh.goal == "repair the connector call"
    assert fresh.exclusions == ["do not expand the goal"]
    assert fresh.input_refs == ["artifact:ev-1"]
    assert fresh.required_capabilities == ["mcp.exec"]
    assert fresh.retry_policy == "bounded"
    assert fresh.agent_version == "v1" and fresh.policy_version == "p1"


# ── §15.4: authority is the intersection; prompts grant nothing ────────

def test_prompt_declared_role_grants_nothing():
    """A builder claiming to be an administrator in its prompt gains no
    activation authority — the role comes from the DELEGATION, and the
    effective set is the intersection."""
    from backend.services.agent_authority import effective_authority
    from backend.models.agent_task import AgentRole
    # The recovery analyst's whitelist has no activation tool at all —
    # no intersection can grant one, whatever the prompt says.
    auth = effective_authority(
        role=AgentRole.RECOVERY_ANALYST,
        parent_allowed_tools=["evidence.read", "mcp.activate"],
        user_permissions=["evidence.read", "mcp.activate", "mcp.exec"])
    assert auth.allows("mcp.activate") is False, \
        "the role whitelist has no activation tool — no intersection " \
        "can grant one, whatever the prompt claims"
    assert auth.tools == frozenset({"evidence.read"})


def test_child_cannot_exceed_parent_scope_or_user_permission():
    from backend.services.agent_authority import effective_authority
    from backend.models.agent_task import AgentRole
    # The parent's scope excludes connector.test even though the role
    # and the user allow it.
    auth = effective_authority(
        role=AgentRole.CONNECTOR_VALIDATOR,
        parent_allowed_tools=["connector.read"],
        user_permissions=["connector.read", "connector.test"])
    assert auth.allows("connector.test") is False
    assert auth.allows("connector.read") is True
    # The user's permission excludes evidence.read even though the role
    # and parent allow it.
    auth2 = effective_authority(
        role=AgentRole.OUTCOME_VERIFIER,
        parent_allowed_tools=["evidence.read", "artifact.resolve"],
        user_permissions=["artifact.resolve"])
    assert auth2.allows("evidence.read") is False


def test_approval_gated_tools_need_a_current_grant():
    from backend.services.agent_authority import effective_authority
    from backend.models.agent_task import AgentRole
    auth = effective_authority(
        role=AgentRole.BROWSER_NAVIGATOR,
        parent_allowed_tools=["browser.act", "browser.observe"],
        user_permissions=["browser.act", "browser.observe"],
        tool_execution_policy={"browser.act": "approval_required"})
    assert auth.allows("browser.act") is False, "no approval yet"
    assert "browser.act" in auth.requires_approval_for
    auth2 = effective_authority(
        role=AgentRole.BROWSER_NAVIGATOR,
        parent_allowed_tools=["browser.act", "browser.observe"],
        user_permissions=["browser.act", "browser.observe"],
        tool_execution_policy={"browser.act": "approval_required"},
        pending_approvals=["browser.act"])
    assert auth2.allows("browser.act") is True


# ── §15.5: authorization rechecked at execution and resume ─────────────

@pytest.mark.asyncio
async def test_revoked_membership_stops_queued_work_as_access_loss(repo):
    """Removing a user stops their queued specialist work with the
    DISTINCT access-loss outcome — not a transient retry."""
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    from backend.services.agent_authority import AuthorizationLost
    ctx = VerificationContext(
        task_id="t1", run_id="run-rev", goal="g", criteria=[],
        output_data={"reply": "ok"}, deterministic_verdict="pass",
        deterministic_reasons=[])
    with pytest.raises(AuthorizationLost, match="membership was revoked"):
        await run_verifier_via_contract(
            repo, ctx, parent_id="run-rev",
            principal_id="principal-owner",
            authorization_context={"membership_current": False})
    # NO contract was created — the work stopped before delegation.
    contracts = await repo.subtree_of("run-rev")
    assert contracts == []


@pytest.mark.asyncio
async def test_credential_scope_loss_is_distinct_from_transient(repo):
    from backend.agents.connector_validator.validator_agent import (
        ValidationContext, run_validator_via_contract)
    from backend.services.agent_authority import AuthorizationLost
    ctx = ValidationContext(connector_id="m1", operation_id="op")
    with pytest.raises(AuthorizationLost) as exc:
        await run_validator_via_contract(
            repo, ctx, parent_id="run-cred",
            principal_id="principal-owner",
            authorization_context={"credential_scope_valid": False})
    assert exc.value.reason == "credential_scope_invalid"
    assert exc.value.drop_cached is True
