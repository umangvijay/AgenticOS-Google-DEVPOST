"""Phase 14 finding H: required-operation propagation — UNDECLARED ≠
EMPTY, and a task-initiated build cannot claim empty coverage of what
its originating task was trying to do.

- The orchestrator's build tool parses and propagates the operation
  declaration into the durable build row AND the build task input.
- The readiness gate refuses a task-originated build whose declaration
  is empty (the needs were never named).
- An API-originated build with an explicit empty declaration is the
  operator's choice and stays ready (the distinction is by origin).
"""
import json

import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager


# ── The orchestrator parses the declaration ────────────────────────────

def test_orchestrator_parses_comma_separated_operations():
    from backend.agents.orchestrator.orchestrator_agent import (
        _parse_required_operations)
    assert _parse_required_operations("create_invoice, list_invoices") == \
        ["create_invoice", "list_invoices"]
    assert _parse_required_operations("") is None
    assert _parse_required_operations("  ") is None
    assert _parse_required_operations("one") == ["one"]


@pytest.mark.asyncio
async def test_task_initiated_build_carries_declared_operations(tmp_path):
    """The declaration travels: build row + build task input_data."""
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)

    class _FakeMCPRepo:
        async def create_build(self, build):
            return build

    import uuid as _uuid
    uid = _uuid.uuid4().hex[:8]
    db = DatabaseManager(str(tmp_path / "ops.db"))
    await db.initialize()
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    await factory.initialize()
    try:
        dispatch = await request_durable_build(
            factory, user_id=f"u-ops-{uid}", method="prompt",
            source=f"Acme API {uid}: invoices", name="acme",
            origin=ORIGIN_ORCHESTRATOR,
            parent_run_id="run-p", parent_workflow_id="wf-p",
            parent_task_id="task-p",
            required_operations=["create_invoice", "list_invoices"])
        build = await factory.mcp_repo.get_build(dispatch["build_id"])
        assert build["required_operations"] == ["create_invoice",
                                                "list_invoices"]
        assert build["parent_task_id"] == "task-p"
        # The build TASK's input carries the declaration too.
        cmd_repo = factory.command_repo
        command = await cmd_repo.get_command(dispatch["command_id"])
        assert command.payload.get("required_operations") == [
            "create_invoice", "list_invoices"]
    finally:
        await factory.shutdown()
        await db.close()


# ── The readiness gate: task-originated builds cannot declare empty ────

def _build(**overrides):
    base = {"build_id": "b-h", "status": "success", "mcp_id": "mcp-h",
            "credential_reference": "k",
            "required_operations": []}
    base.update(overrides)
    return base


def _mcp():
    return {"is_enabled": 1, "active_revision_id": "rev-h",
            "auth_json": '{"credential_name": "k"}'}


def test_task_originated_build_with_empty_declaration_is_not_ready():
    """§14.H: a parked task's build with an empty declaration has NOT
    declared coverage — readiness refuses and names the gap."""
    from backend.services.connector_eligibility import (
        CAPABILITY_CREDENTIALS_READY, evaluate_capability_readiness)
    out = evaluate_capability_readiness(
        _mcp(), _build(parent_task_id="task-p"))
    assert out["ready"] is False
    assert out["state"] == CAPABILITY_CREDENTIALS_READY
    assert "declare the operations the originating task requires" \
        in out["next_action"]
    assert out["detail"]["required_operations"] == "undeclared_for_task"


def test_api_originated_empty_declaration_stays_explicit_choice():
    """An API-originated build with an explicit [] declaration is the
    operator's statement that nothing is required — it stays eligible
    (the gate distinguishes by ORIGIN, not by paranoia)."""
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    out = evaluate_capability_readiness(_mcp(), _build())
    assert out["ready"] is True


def test_task_originated_with_declared_ops_validates_those_ops():
    """The positive path: a task-initiated build WITH a declaration is
    gated on live validation of exactly those operations."""
    from backend.models.admission import epoch_now
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    runs = [{"operation_id": "create_invoice", "status": "passed",
             "revision_id": "rev-h", "credential_binding_id": "b1",
             "valid_until": epoch_now() + 3600}]
    # Declared but only ONE of two validated → refused, naming the gap.
    out = evaluate_capability_readiness(
        _mcp(), _build(parent_task_id="task-p",
                       required_operations=["create_invoice",
                                            "list_invoices"]),
        runs)
    assert out["ready"] is False
    assert out["detail"]["missing_operations"] == ["list_invoices"]
    # Both validated → ready.
    runs.append({"operation_id": "list_invoices", "status": "passed",
                 "revision_id": "rev-h", "credential_binding_id": "b1",
                 "valid_until": epoch_now() + 3600})
    out = evaluate_capability_readiness(
        _mcp(), _build(parent_task_id="task-p",
                       required_operations=["create_invoice",
                                            "list_invoices"]),
        runs)
    assert out["ready"] is True
