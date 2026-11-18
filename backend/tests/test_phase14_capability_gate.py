"""Phase 14 §14.4: the capability-ready gate as the WORKER drives it —
a parent parked on a capability build resumes only when the capability
is READY under current policy (registered, credential-bound, required
operations validated on the active revision, revision ACTIVATED), and
the gate's verdict composes with the §14.H declaration rules.
"""
from backend.models.admission import epoch_now


def _gate(mcp, build, runs):
    """The exact call the worker makes (worker.py `_resume_build_waiter`)."""
    from backend.services.connector_eligibility import (
        evaluate_capability_readiness)
    return evaluate_capability_readiness(mcp, build, runs)


def _build(**over):
    b = {"build_id": "b-14.4", "status": "success", "mcp_id": "mcp-14.4",
         "credential_reference": "acme-key", "required_operations": [],
         "parent_run_id": "run-p", "parent_task_id": "task-p"}
    b.update(over)
    return b


def _passing_run(op, rev="rev-1"):
    return {"operation_id": op, "status": "passed", "revision_id": rev,
            "credential_binding_id": "bind-1",
            "valid_until": epoch_now() + 3600}


def test_generation_success_alone_never_resumes_the_parent():
    """A build that generated but registered NOTHING ready keeps the
    parent parked with the precise next action (§12.7 through §14.4)."""
    out = _gate(None, _build(), [])   # no mcp record at all
    assert out["ready"] is False
    assert out["next_action"], "the parked parent gets a precise next action"


def test_full_ready_ladder_resumes():
    """Registered + enabled + credential-bound + operations validated on
    the ACTIVE revision + activated → ready."""
    mcp = {"is_enabled": 1, "active_revision_id": "rev-1",
           "auth_json": '{"credential_name": "acme-key"}'}
    out = _gate(mcp, _build(required_operations=["list_invoices"]),
                [_passing_run("list_invoices")])
    assert out["ready"] is True


def test_untested_declared_operation_parks_with_named_gap():
    """The declared-but-unvalidated operation names itself in the next
    action — the parent stays parked, the candidate is preserved."""
    mcp = {"is_enabled": 1, "active_revision_id": "rev-1",
           "auth_json": '{"credential_name": "acme-key"}'}
    out = _gate(mcp, _build(required_operations=["create_invoice",
                                                "list_invoices"]),
                [_passing_run("list_invoices")])
    assert out["ready"] is False
    assert "create_invoice" in out["next_action"]
    assert out["detail"]["missing_operations"] == ["create_invoice"]


def test_task_originated_build_requires_declaration_even_when_enabled():
    """§14.4 × §14.H composition: a task-initiated build with an empty
    declaration parks even though the connector itself is healthy — the
    task's needs were never named."""
    mcp = {"is_enabled": 1, "active_revision_id": "rev-1",
           "auth_json": '{"credential_name": "acme-key"}'}
    out = _gate(mcp, _build(parent_task_id="task-p"), [])
    assert out["ready"] is False
    assert out["detail"]["required_operations"] == "undeclared_for_task"


def test_stale_validation_on_old_revision_cannot_resume():
    """Validation evidence from a SUPERSEDED revision is not evidence —
    the parent stays parked until the ACTIVE revision validates."""
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "acme-key"}'}
    out = _gate(mcp, _build(required_operations=["list_invoices"]),
                [_passing_run("list_invoices", rev="rev-1")])
    assert out["ready"] is False
    assert out["detail"]["missing_operations"] == ["list_invoices"]
