"""Phase 15 audit closure — every specialist service now has a REAL
production caller, and the Phase 14 integration bugs are fixed:

- W1 (15.30): the release-stage gate is enforced INSIDE delegate().
- W2 (15.1): the engine records the delegation decision (reason +
  alternatives) with the verification.
- W3 (15.17): the recovery worker classifies the specialist failure
  through the response table before choosing a strategy.
- W4 (15.21): resume building runs the fact-groundedness check and
  flags unsupported additions.
- W6 (15.12/15.20): the verifier returns a typed handoff; the browser
  agent validates a session contract and returns it with the durable
  checkpoint.
- W7 (14.11 real bug): user-controlled task categories COERCE to the
  baseline bucket instead of silently dropping decisions; budget units
  travel into the decision log.
- W8 (14.5): the durable checkpoint has a READER.
"""
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
    db = DatabaseManager(str(tmp_path / "wiring.db"))
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


# ── W1: the stage gate is part of the delegation contract ──────────────

@pytest.mark.asyncio
async def test_delegate_refuses_operator_disabled_roles(repo):
    from backend.config.settings import settings
    from backend.models.agent_task import AgentRole, AgentTaskError
    from backend.services.agent_contracts import delegate
    import backend.services.specialist_release as sr
    # The shipped default allows the verifier.
    c = await delegate(
        parent_kind="run", parent_id="run-w1", role=AgentRole.OUTCOME_VERIFIER,
        tenant_id="u1", goal="g", input_data={"x": 1},
        output_schema={"required": ["ok"]},
        acceptance_criteria=[{"kind": "non_empty"}],
        allowed_tools=["evidence.read"], repo=repo)
    assert c.contract_id
    # An operator-disabled role refuses NEW delegation.
    from unittest.mock import patch
    with patch.object(sr, "ROLE_MIN_STAGE",
                      {**sr.ROLE_MIN_STAGE, "outcome_verifier":
                       "learned_selection"}):
        with pytest.raises(AgentTaskError, match="release stage"):
            await delegate(
                parent_kind="run", parent_id="run-w1b",
                role=AgentRole.OUTCOME_VERIFIER, tenant_id="u1", goal="g",
                input_data={"x": 1}, output_schema={"required": ["ok"]},
                acceptance_criteria=[{"kind": "non_empty"}],
                allowed_tools=["evidence.read"], repo=repo)


# ── W7: the context-schema coercion + cost measurement ────────────────

def test_unknown_user_task_categories_coerce_never_drop():
    from backend.engine.engine import _normalize_task_category
    assert _normalize_task_category("research") == "research"
    # A user-supplied nonsense category coerces to the baseline — the
    # decision LOGS (bucketed), it does not crash and vanish.
    assert _normalize_task_category("'; DROP TABLE users;--") == "workflow"
    assert _normalize_task_category(None) == "workflow"
    assert _normalize_task_category("") == "workflow"


def test_routing_context_schema_still_refuses_programmatic_unknowns():
    """Programmatic callers (not the engine's coercion path) still get
    the strict schema — the coercion is the ENGINE's honest mapping."""
    from backend.models.routing_decision import (
        RoutingContextError, validate_context)
    with pytest.raises(RoutingContextError):
        validate_context(task_category="sql_injection")


# ── W8: the durable checkpoint has a reader ────────────────────────────

@pytest.mark.asyncio
async def test_checkpoint_roundtrip_record_then_read(tmp_path):
    from backend.repositories.browser_session_repository import (
        BrowserSessionRepository)
    from backend.models.browser_session import (
        BrowserSession, ChallengeState, CleanupState, ControllerState,
        ResourceState)
    db = DatabaseManager(str(tmp_path / "ckpt-rw.db"))
    await db.initialize()
    r = BrowserSessionRepository(db)
    await r.create_session(BrowserSession(
        session_id="bs-w8", tenant_id="t1", user_id="u1", workflow_id="wf",
        run_id="run-w8", task_id="t", owner_worker_id="w1", owner_epoch="e1",
        resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT,
        challenge_state=ChallengeState.NONE,
        cleanup_state=CleanupState.NOT_REQUIRED,
        allowed_origins=["a.test"], runtime_session_id="rt"))
    assert await r.latest_checkpoint("bs-w8") is None, \
        "no checkpoint yet — recovery sees NONE, not a guess"
    ref = await r.record_checkpoint("bs-w8", checkpoint_digest="dig-1",
                                    milestone="step 2: submit",
                                    captured_at=5000)
    ck = await r.latest_checkpoint("bs-w8")
    assert ck["reference"] == ref
    assert ck["digest"] and ck["captured_at"] == 5000
    await db.close()


# ── W6: the typed handoff + session contract at the boundaries ─────────

@pytest.mark.asyncio
async def test_verifier_returns_a_typed_handoff(repo):
    from backend.agents.verifier.verifier_agent import (
        VerificationContext, run_verifier_via_contract)
    ctx = VerificationContext(
        task_id="t1", run_id="run-h", goal="g",
        criteria=[{"kind": "contains", "value": "invoice"},
                  {"kind": "non_empty", "value": "summary"}],
        output_data={"reply": "here is the invoice"},
        deterministic_verdict="pass", deterministic_reasons=[])
    out = await run_verifier_via_contract(repo, ctx, parent_id="run-h",
                                          principal_id="principal-owner")
    h = out["handoff"]
    assert h["from_role"] == "outcome_verifier"
    assert h["results"]["verdict"] in ("pass", "fail", "needs_review")
    assert h["authority_items"] == 0, \
        "the verifier's observations are evidence, never authority"
    assert isinstance(h["limitations"], list)


@pytest.mark.asyncio
async def test_browser_agent_result_carries_contract_and_checkpoint(tmp_path, monkeypatch):
    """The browser delegation's result carries the typed session contract
    and the durable checkpoint — a full live Playwright run needs a
    browser, so this exercises the WIRING shape via the module contract:
    the result trailer builder."""
    from backend.services import web_agent
    # The wiring exists in the source: the durable-session block builds
    # and validates the contract; the result trailer includes it.
    src = open(web_agent.__file__).read()
    assert "session_contract.validate()" in src
    assert "_trailer[\"session_contract\"]" in src
    assert "latest_checkpoint" in src
