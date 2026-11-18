"""Phase 13 chat-8 acceptance tests (§13.22/§13.23/§13.28/§13.29)."""
import pytest
import pytest_asyncio

from backend.repositories.recovery_episode_repository import (
    RecoveryEpisodeRepository, RecoveryState)
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.recovery_improvement import (
    propose_connector_revision_improvement, recovery_metrics)


@pytest_asyncio.fixture
async def env(tmp_path):
    db = DatabaseManager(str(tmp_path / "chat8.db"))
    await db.initialize()
    ep_repo = RecoveryEpisodeRepository(db)
    try:
        yield {"db": db, "episodes": ep_repo}
    finally:
        await db.close()


async def _episode(repo, episode_id, run_id, task_id, state, category="timeout",
                   attempts=1):
    await repo.create_episode({
        "episode_id": episode_id, "tenant_id": "u1", "actor_id": "system",
        "run_id": run_id, "task_id": task_id, "failure_code": "TIMEOUT",
        "failure_category": category, "goal_digest": "g",
        "attempt_count": attempts, "deadline_at": 99999999999})
    if state != "DETECTED":
        await repo.transition(episode_id, RecoveryState.CLASSIFIED, actor="system")
    if state in ("ESCALATED",):
        await repo.transition(episode_id, RecoveryState.ESCALATED, actor="system")
    elif state in ("VERIFIED_RECOVERY",):
        await repo.transition(episode_id, RecoveryState.PROPOSED, actor="agent")
        await repo.transition(episode_id, RecoveryState.VALIDATED, actor="system")
        await repo.transition(episode_id, RecoveryState.READY, actor="system")
        await repo.claim_episode(episode_id, "w1")
        await repo.transition(episode_id, RecoveryState.EXECUTING, actor="agent",
                              require_live_lease=True)
        await repo.transition(episode_id, RecoveryState.VERIFYING, actor="system",
                              require_live_lease=True)
        await repo.transition(episode_id, RecoveryState.VERIFIED_RECOVERY,
                              actor="verifier", require_live_lease=True)
    elif state in ("RECOVERY_FAILED",):
        await repo.transition(episode_id, RecoveryState.PROPOSED, actor="agent")
        await repo.transition(episode_id, RecoveryState.VALIDATED, actor="system")
        await repo.transition(episode_id, RecoveryState.READY, actor="system")
        await repo.claim_episode(episode_id, "w1")
        await repo.transition(episode_id, RecoveryState.EXECUTING, actor="agent",
                              require_live_lease=True)
        await repo.transition(episode_id, RecoveryState.RECOVERY_FAILED,
                              actor="system", require_live_lease=True)


# ── §13.22: recovery measured by failure class ─────────────────────────

@pytest.mark.asyncio
async def test_recovery_credit_requires_verified_state(env):
    """THE §13.22 acceptance: 'repair queued' and 'retry attempted' never
    count as healed — credit requires VERIFIED_RECOVERY."""
    repo = env["episodes"]
    await _episode(repo, "ep-v1", "run-v1", "t1", "VERIFIED_RECOVERY",
                   category="timeout")
    await _episode(repo, "ep-q1", "run-q1", "t2", "ESCALATED",
                   category="timeout")   # repair queued — NOT healed
    await _episode(repo, "ep-f1", "run-f1", "t3", "RECOVERY_FAILED",
                   category="timeout")
    metrics = await recovery_metrics(repo, tenant_id="u1")
    assert metrics["verified_recoveries"] == 1
    assert metrics["total_episodes"] == 3
    assert metrics["verified_recovery_rate"] == pytest.approx(0.3333, abs=1e-3)
    assert "never count as healed" in metrics["note"]
    by_class = metrics["by_failure_class"]["timeout"]
    assert by_class["verified_recoveries"] == 1
    assert by_class["escalated"] == 1
    assert by_class["failed_recoveries"] == 1


@pytest.mark.asyncio
async def test_repeat_failure_rate_measured(env):
    repo = env["episodes"]
    await _episode(repo, "ep-r1", "run-r1", "t1", "VERIFIED_RECOVERY",
                   category="schema", attempts=1)
    await _episode(repo, "ep-r2", "run-r2", "t2", "VERIFIED_RECOVERY",
                   category="schema", attempts=3)   # repeated failure
    metrics = await recovery_metrics(repo, tenant_id="u1")
    assert metrics["repeat_failure_rate"] == pytest.approx(0.5)


# ── §13.23: connector revision improvement ─────────────────────────────

@pytest.mark.asyncio
async def test_connector_revision_proposal_is_unavailable_until_gates(env):
    """§13.23: the candidate revision is created via checkpoint machinery
    and stays UNAVAILABLE — the proposal activates nothing."""
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        from backend.services.connector_revisions import checkpoint
        mcp_id = "mcp-rev-imp"
        await factory.mcp_repo.register_mcp({
            "mcp_id": mcp_id, "name": "acme", "version": "1.0",
            "endpoint": "internal://acme", "transport": "internal",
            "owner": "u1", "description": "test connector",
            "tools": [{"tool_name": "list_invoices"}],
        })
        await factory.mcp_repo.cache_tools(mcp_id, [
            {"tool_name": "list_invoices"}])
        revision = await checkpoint(factory.mcp_repo, mcp_id)
        assert revision.get("revision_id")

        out = await propose_connector_revision_improvement(
            mcp_repo=factory.mcp_repo, mcp_id=mcp_id,
            revision_service=__import__(
                "backend.services.connector_revisions",
                fromlist=["checkpoint"]),
            reviewed_failures=[{"task_id": "list_invoices",
                                "failure_category": "schema_drift"}],
            proposed_by="operator:a")
        assert out["state"] == "candidate_unavailable"
        assert out["affected_operations"] == ["list_invoices"]
        assert "activates nothing" in out["note"]
    finally:
        await factory.shutdown()


@pytest.mark.asyncio
async def test_connector_revision_permission_expansion_refused(env):
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        mcp_id = "mcp-rev-exp"
        await factory.mcp_repo.register_mcp({
            "mcp_id": mcp_id, "name": "acme", "version": "1.0",
            "endpoint": "internal://acme", "transport": "internal",
            "owner": "u1", "description": "test connector",
            "tools": [{"tool_name": "list_invoices"}],
        })
        await factory.mcp_repo.cache_tools(mcp_id, [
            {"tool_name": "list_invoices"}])
        with pytest.raises(PermissionError, match="expands operations"):
            await propose_connector_revision_improvement(
                mcp_repo=factory.mcp_repo, mcp_id=mcp_id,
                revision_service=__import__(
                    "backend.services.connector_revisions",
                    fromlist=["checkpoint"]),
                reviewed_failures=[{"task_id": "list_invoices"}],
                proposed_by="operator:a",
                candidate_definition={"tools": [
                    {"tool_name": "list_invoices"},
                    {"tool_name": "delete_everything"}]})
    finally:
        await factory.shutdown()


# ── §13.28: specialist agents through bounded contracts ────────────────

@pytest.mark.asyncio
async def test_new_specialist_roles_delegate_within_boundaries(tmp_path):
    """§13.28: the review's specialist table — each new role delegates
    through the SAME bounded contracts with its authority boundary."""
    from backend.models.agent_task import AgentRole
    from backend.repositories.agent_task_repository import (
        AgentTaskRepository, set_agent_task_repo)
    from backend.services.agent_contracts import delegate
    db = DatabaseManager(str(tmp_path / "specialists.db"))
    await db.initialize()
    repo = AgentTaskRepository(db)
    set_agent_task_repo(repo)
    try:
        specialists = [
            (AgentRole.EVALUATION_CURATOR, ["evidence.read", "dataset.propose"]),
            (AgentRole.IMPROVEMENT_PROPOSER, ["evidence.read",
                                              "experiment.propose"]),
            (AgentRole.RELEASE_VERIFIER, ["evidence.read", "release.assemble"]),
            (AgentRole.RECONCILIATION, ["evidence.read", "effect.reconcile"]),
        ]
        for role, tools in specialists:
            # §15.3: complete contracts — goal, criteria, authority.
            contract = await delegate(
                parent_kind="run", parent_id="run-spec", role=role,
                tenant_id="u1", goal=f"perform {role.value} work",
                input_data={"task": role.value},
                acceptance_criteria=[{"kind": "non_empty",
                                      "description": "typed output exists"}],
                allowed_tools=tools,
                dedup_key=f"{role.value}-k", repo=repo)
            assert contract.state.value == "PROPOSED"
        # A specialist cannot hold tools outside its boundary.
        from backend.models.agent_task import AgentTaskError
        with pytest.raises(AgentTaskError, match="authority boundary"):
            await delegate(
                parent_kind="run", parent_id="run-spec",
                role=AgentRole.EVALUATION_CURATOR, tenant_id="u1",
                input_data={}, allowed_tools=["mcp.dispatch"],
                dedup_key="bad-k", repo=repo)
    finally:
        set_agent_task_repo(None)
        await db.close()


# ── §13.29: deliverable quality connected to verification ──────────────

@pytest.mark.asyncio
async def test_deliverable_verification_uses_artifact_service(tmp_path):
    """§13.29: a run cannot be verified solely because a download link
    exists — the artifact verifier resolves through the authorized
    registry with ownership, checksum, and format checks."""
    from backend.services.deliverable_verification import (
        DeliverableRegistry, verify_deliverable)
    registry = DeliverableRegistry()
    # Missing artifact: fails, never passes silently.
    out = await verify_deliverable("art-missing", owner_user_id="u1",
                                   registry=registry)
    assert out["verified"] is False and out["reason"] == "not_found"
    # Registered artifact: verifies with checksum + ownership.
    await registry.register("art-1", owner_user_id="u1",
                            content={"file": "report.pdf", "size": 10},
                            content_type="application/pdf")
    ok = await verify_deliverable("art-1", owner_user_id="u1",
                                  registry=registry)
    assert ok["verified"] is True and ok["checksum"]
    # Wrong owner: refused.
    denied = await verify_deliverable("art-1", owner_user_id="attacker",
                                      registry=registry)
    assert denied["verified"] is False and denied["reason"] == "not_found"
