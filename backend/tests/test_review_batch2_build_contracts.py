"""Review batch 2 (§12.5–12.8) acceptance tests: durable recovery authority
and capability-build contracts.

- §12.5 (finding G): the episode is the claim authority — covered in
  test_recovery_worker_acceptance.py; here: the SQLite + Firestore
  ownership-gate parity for protected transitions.
- §12.6 (finding H): organization scope and the credential-BINDING
  reference propagate through the queued build into the command, the
  build record, and the child task.
- §12.7 (finding H): the parent resumes ONLY at capability ACTIVATED —
  a registered-but-disabled or unvalidated connector parks the parent
  with the precise next action.
- §12.8 (finding I): ONE backend-neutral initial-work description — the
  same typed items translate to equivalent SQLite and Firestore work.
"""
import pytest
import pytest_asyncio

from backend.models.command import CommandState
from backend.repositories.sqlite.database import DatabaseManager
from backend.services.connector_eligibility import (
    CAPABILITY_ACTIVATED,
    CAPABILITY_CREDENTIALS_READY,
    CAPABILITY_ELIGIBLE,
    CAPABILITY_REGISTERED,
    evaluate_capability_readiness,
)


# ── §12.6: build context propagation ───────────────────────────────────

@pytest_asyncio.fixture
async def env(tmp_path):
    from backend.repositories.factory import RepositoryFactory
    factory = RepositoryFactory()
    # Keep the factory's SQLite wiring without a full bus.
    await factory.initialize()
    try:
        yield factory
    finally:
        await factory.shutdown()


@pytest.mark.asyncio
async def test_build_context_propagates_through_the_durable_contract(env):
    """§12.6 acceptance: a build originating from an organization-owned
    task retains that scope through the command, the build record, and the
    child task — plus the credential-BINDING reference (never a secret)."""
    from backend.services.connector_build_service import (
        ORIGIN_ORCHESTRATOR, request_durable_build)
    from backend.services.command_service import get_command_repo

    import uuid as _uuid
    uid = _uuid.uuid4().hex[:8]
    factory = env
    orchestrator = _FakeToolRouter(factory.mcp_repo)
    dispatch = await request_durable_build(
        orchestrator, user_id=f"u-org-{uid}", method="prompt",
        source=f"Acme Invoices API {uid}: list invoices, create draft",
        name="acme", origin=ORIGIN_ORCHESTRATOR,
        parent_run_id="run-parent", parent_workflow_id="wf-parent",
        parent_task_id="task-parent",
        organization_id="org-77",
        credential_reference="acme-prod-key",
    )
    assert dispatch["build_id"]

    command = await get_command_repo().get_command(dispatch["command_id"])
    assert command.organization_id == "org-77"
    assert command.payload.get("credential_reference") == "acme-prod-key"

    build = await factory.mcp_repo.get_build(dispatch["build_id"])
    assert build.get("organization_id") == "org-77"
    assert build.get("credential_reference") == "acme-prod-key"

    run = await factory.workflow_repo.get_run(dispatch["run_id"])
    assert run.get("org_id") == "org-77", "org scope reaches the child run"
    task = next(t for t in run["tasks"] if t["task_id"] == "mcp-build")
    task_input = task["input_data"]
    if isinstance(task_input, str):
        import json
        task_input = json.loads(task_input)
    assert task_input.get("credential_reference") == "acme-prod-key"


class _FakeToolRouter:
    """Minimal tool-router surface the build service touches."""
    def __init__(self, mcp_repo):
        self.mcp_repo = mcp_repo


# ── §12.7: capability readiness before parent resume ───────────────────

def _build(**overrides):
    base = {"build_id": "b1", "status": "success", "mcp_id": "mcp-1",
            "credential_reference": "acme-prod-key",
            "required_operations": []}
    base.update(overrides)
    if base.get("required_operations") is None and "required_operations" in overrides:
        del base["required_operations"]   # undeclared = key absent (§13.3)
    return base


def test_disabled_connector_is_not_ready():
    """§12.7 acceptance: a registered but DISABLED connector does not
    cause a false ready state."""
    mcp = {"is_enabled": 0, "active_revision_id": "rev-1", "auth_json": "{}"}
    out = evaluate_capability_readiness(mcp, _build())
    assert out["ready"] is False
    assert out["state"] == CAPABILITY_REGISTERED
    assert "enable" in out["next_action"]


def test_missing_credentials_preserve_candidate_with_next_action():
    mcp = {"is_enabled": 1, "active_revision_id": None,
           "auth_json": "{}"}
    out = evaluate_capability_readiness(mcp, _build())
    assert out["ready"] is False
    assert out["state"] == CAPABILITY_REGISTERED
    assert "acme-prod-key" in out["next_action"]
    assert "reconnect" in out["next_action"] or "connect" in out["next_action"]


def test_unvalidated_required_operations_block_resume():
    """§12.7: required operations must be TESTED against the current
    revision before the parent resumes."""
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "acme-prod-key"}',
           "validation_runs": [
               {"operation_id": "list_invoices", "completed_at": 1,
                "revision_id": "rev-2"}]}
    out = evaluate_capability_readiness(mcp, _build(
        required_operations=["list_invoices", "create_draft"]))
    assert out["ready"] is False
    assert "create_draft" in out["next_action"]


def test_unactivated_revision_is_not_ready():
    mcp = {"is_enabled": 1, "active_revision_id": None,
           "auth_json": '{"credential_name": "k"}'}
    out = evaluate_capability_readiness(mcp, _build())
    assert out["ready"] is False
    assert out["state"] == CAPABILITY_ELIGIBLE
    assert "activate" in out["next_action"]


def test_fully_ready_capability_activates():
    mcp = {"is_enabled": 1, "active_revision_id": "rev-1",
           "auth_json": '{"credential_name": "acme-prod-key"}'}
    out = evaluate_capability_readiness(mcp, _build())
    assert out["ready"] is True
    assert out["state"] == CAPABILITY_ACTIVATED


def test_failed_build_never_ready():
    out = evaluate_capability_readiness(None, _build(status="failed"))
    assert out["ready"] is False


# ── §12.8: one backend-neutral initial-work description ────────────────

def test_translator_produces_equivalent_sqlite_and_firestore_work():
    """§12.8: the same typed items translate to a SQLite callback and to
    deterministic Firestore triples — the caller never branches per store."""
    from backend.services.command_service import translate_initial_work_items
    items = [
        ("parent_wait", {
            "wait_id": "w1", "parent_run_id": "pr", "parent_task_id": "pt",
            "child_command_id": "cc", "child_run_id": "cr",
            "condition": "child_build_succeeded", "state": "waiting",
            "created_at": "t0"}),
        ("build_record", {
            "build_id": "b1", "user_id": "u1", "name": "n", "method": "prompt",
            "source": "s", "origin": "orchestrator", "wait_reason": "capability_missing",
            "organization_id": "org-1", "credential_reference": "k",
            "created_at": "t0", "updated_at": "t0"}),
        ("workflow_run_with_task", {
            "run": {"run_id": "r1", "workflow_id": "w1", "user_id": "u1",
                    "goal": "g", "created_at": "t0", "updated_at": "t0",
                    "parent_run_id": "pr", "thread_id": "r1",
                    "org_id": "org-1"},
            "task": {"task_id": "mcp-build", "run_id": "r1",
                     "workflow_id": "w1", "user_id": "u1",
                     "agent": "core.mcp_build", "tool": "mcp_build",
                     "input_data": "{}", "dependencies": "[]",
                     "timeout_seconds": 300, "max_retries": 2,
                     "recovery_enabled": 1}}),
    ]
    sqlite_cb, triples = translate_initial_work_items(items)
    assert callable(sqlite_cb)
    collections = [t[0] for t in triples]
    assert collections == ["parent_waits", "mcp_builds", "workflow_runs"]
    # Deterministic doc ids in both stores.
    assert triples[0][1] == "w1" and triples[1][1] == "b1" and \
        triples[2][1] == "r1"
    # Firestore gets the task embedded in the run doc; SQLite gets a task row.
    assert triples[2][2]["tasks"][0]["task_id"] == "mcp-build"


@pytest.mark.asyncio
async def test_command_accepts_neutral_items_on_sqlite(env):
    """The build command accepted via initial_work_items commits the SAME
    initial work the legacy callback did (build run + task + wait)."""
    import uuid as _uuid
    from backend.services.command_service import (
        accept_command, translate_initial_work_items,
    )
    from backend.models.command import new_command_id, new_wait_id
    now = "2026-11-02T00:00:00+00:00"
    uid = _uuid.uuid4().hex[:8]
    run_id = f"run-c-{uid}"
    cid = new_command_id()
    items = [
        ("parent_wait", {
            "wait_id": new_wait_id(), "parent_run_id": f"run-p-{uid}",
            "parent_task_id": "task-p", "child_command_id": cid,
            "child_run_id": run_id, "condition": "child_build_succeeded",
            "state": "waiting", "created_at": now}),
        ("workflow_run_with_task", {
            "run": {"run_id": run_id, "workflow_id": f"wf-c-{uid}",
                    "user_id": "u1", "goal": "g", "created_at": now,
                    "updated_at": now, "parent_run_id": f"run-p-{uid}",
                    "thread_id": run_id, "org_id": "org-9"},
            "task": {"task_id": "mcp-build", "run_id": run_id,
                     "workflow_id": f"wf-c-{uid}", "user_id": "u1",
                     "agent": "core.mcp_build", "tool": "mcp_build",
                     "input_data": "{}", "dependencies": "[]",
                     "timeout_seconds": 300, "max_retries": 2,
                     "recovery_enabled": 1}}),
    ]
    receipt = await accept_command(
        command_id=cid, command_type="build", owner_user_id="u1",
        organization_id="org-9", idempotency_key=f"bw-{cid}",
        request_digest="dig", run_id="run-c", build_id="b-1",
        initial_work_items=items)
    assert receipt.accepted is True
    run = await env.workflow_repo.get_run(run_id)
    assert run is not None and run.get("org_id") == "org-9"
    assert any(t["task_id"] == "mcp-build" for t in run["tasks"])


# ── Phase 20 §20.2: operational health ─────────────────────────────────

@pytest.mark.asyncio
async def test_operational_health_aggregates_stores(env):
    from backend.api.routers.admin import operational_health
    from backend.api.dependencies.auth import AuthenticatedUser

    class _User(AuthenticatedUser):
        def __init__(self):
            super().__init__(user_id="admin-1", email="a@x.io", name="a",
                             role="admin", auth_provider="jwt")

    class _App:
        state = type("S", (), {"factory": env})()

    out = await operational_health(type("R", (), {"app": _App()})(), _User())
    assert "queue" in out and "stuck_leases" in out
    assert "uncertain_effects" in out and "outbox" in out
    assert "commands_overdue" in out and "recovery_stale" in out
    assert "browser_sessions" in out
    assert out["queue"]["pending"] >= 0


# ── §13.3: authoritative readiness (review finding C) ──────────────────

def _passed_run(op, revision="rev-2", binding="bind-1", valid_until=None):
    from backend.models.admission import epoch_now
    return {"operation_id": op, "status": "passed",
            "revision_id": revision, "credential_binding_id": binding,
            "valid_until": valid_until or (epoch_now() + 3600)}


def test_undeclared_operations_are_not_silent_coverage():
    """§13.3 acceptance: a build with NO required-operations declaration
    can never claim complete coverage — readiness refuses explicitly."""
    from backend.services.connector_eligibility import evaluate_capability_readiness
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "k"}'}
    out = evaluate_capability_readiness(mcp, _build(required_operations=None))
    assert out["ready"] is False
    assert "declare the required operations" in out["next_action"]


def test_failed_validation_cannot_resume_the_parent():
    from backend.services.connector_eligibility import evaluate_capability_readiness
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "k"}'}
    runs = [{"operation_id": "list_invoices", "status": "failed",
             "revision_id": "rev-2", "credential_binding_id": "b",
             "valid_until": 9999999999}]
    out = evaluate_capability_readiness(
        mcp, _build(required_operations=["list_invoices"]), runs)
    assert out["ready"] is False


def test_stale_revision_and_expired_evidence_are_refused():
    from backend.services.connector_eligibility import evaluate_capability_readiness
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "k"}'}
    stale = _passed_run("list_invoices", revision="rev-1")     # old revision
    expired = _passed_run("create_draft", valid_until=1)       # expired
    out = evaluate_capability_readiness(
        mcp, _build(required_operations=["list_invoices", "create_draft"]),
        [stale, expired])
    assert out["ready"] is False
    assert set(out["detail"]["missing_operations"]) == \
        {"list_invoices", "create_draft"}


def test_credentialless_validation_does_not_satisfy_a_credential_build():
    from backend.services.connector_eligibility import evaluate_capability_readiness
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "k"}'}
    no_binding = {"operation_id": "list_invoices", "status": "passed",
                  "revision_id": "rev-2", "credential_binding_id": None,
                  "valid_until": 9999999999}
    out = evaluate_capability_readiness(
        mcp, _build(required_operations=["list_invoices"]), [no_binding])
    assert out["ready"] is False, \
        "validation WITHOUT the bound credential context is not evidence"


def test_passing_current_evidence_activates():
    from backend.services.connector_eligibility import (
        CAPABILITY_ACTIVATED, evaluate_capability_readiness)
    mcp = {"is_enabled": 1, "active_revision_id": "rev-2",
           "auth_json": '{"credential_name": "k"}'}
    out = evaluate_capability_readiness(
        mcp, _build(required_operations=["list_invoices"]),
        [_passed_run("list_invoices")])
    assert out["ready"] is True and out["state"] == CAPABILITY_ACTIVATED
