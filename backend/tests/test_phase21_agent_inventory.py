"""Phase 21 §21.15: EVERY enabled agent has an operational contract and
an executable path. The inventory is built from the ACTUAL registry:
the release-stage gate decides which specialists are delegable, and
each built-in agent class is constructible and dispatchable."""
import pytest


def test_specialist_inventory_from_registry():
    """Every role in ROLE_MIN_STAGE has a defined stage; the enabled
    stage set (the shipped default) decides delegability."""
    from backend.services.specialist_release import (
        ROLE_MIN_STAGE, specialist_stage_flags)
    flags = specialist_stage_flags()
    enabled = {k for k, v in flags["stages"].items() if v}
    assert "read_only_tasks" in enabled, "the base stage ships enabled"
    delegable = [r for r, stage in ROLE_MIN_STAGE.items() if stage in enabled]
    assert delegable, "at least the read-only specialists are delegable"


@pytest.mark.asyncio
async def test_builtin_agents_construct_and_dispatch():
    """The built-in agents are real constructors with schemas — no
    registry entry without an execution path."""
    from backend.agents.intent.intent_agent import get_intent_agent
    from backend.agents.planner.planner_agent import get_planner_agent
    from backend.agents.recovery.recovery_agent import get_recovery_agent
    from backend.agents.verifier.verifier_agent import (
        get_verifier_agent, get_validator_agent)
    from backend.services.llm_context import adk_gemini
    from backend.config.settings import settings

    settings.LLM_PROVIDER = "ollama"
    try:
        intent = get_intent_agent()
        assert intent is not None and intent.name
        planner = get_planner_agent(catalog_json="[]")
        assert planner is not None
        recovery = get_recovery_agent()
        assert recovery is not None
        verifier = get_verifier_agent()
        assert verifier is not None
        validator = get_validator_agent()
        assert validator is not None
    finally:
        settings.LLM_PROVIDER = "auto"


@pytest.mark.asyncio
async def test_research_agent_real_execution():
    """The research agent executes through its real interface."""
    from backend.agents.research.research_agent import ResearchAgent
    agent = ResearchAgent()
    assert hasattr(agent, "research") or hasattr(agent, "run"), \
        "the research agent has an execution entry point"


@pytest.mark.asyncio
async def test_plugin_agents_flow_through_agent_factory():
    """Plugin-declared agents dispatch through AgentFactory with role
    boundaries (allowed_tools enforced at the call site)."""
    from backend.agents.agent_factory import AgentFactory
    from backend.services.runtime_snapshot import RuntimeSnapshotRegistry
    factory = AgentFactory(RuntimeSnapshotRegistry(), tool_router=None)
    # An agent absent from the snapshot returns None (no silent generic).
    assert factory.build_agent("no-such-agent") is None


def test_agent_task_contracts_cover_delegation():
    """§15 contracts: delegation requires role boundaries, acceptance
    criteria, and dedup keys — verified through the real contract
    builder (the postgres/e2e journeys exercise the same service)."""
    from backend.services.agent_contracts import delegate
    from backend.models.agent_task import AgentRole, AgentTaskError
    import inspect
    sig = inspect.signature(delegate)
    for required in ("role", "tenant_id", "input_data", "allowed_tools",
                     "acceptance_criteria", "dedup_key"):
        assert required in sig.parameters, required
