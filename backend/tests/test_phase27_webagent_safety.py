"""Phase 27 §27.4 — WebAgent fencing generations and validation
unavailability (audit 27.D2/27.D3)."""
import asyncio
import time
import uuid

import pytest

from backend.services.web_agent import (
    ValidationUnavailableError, WebAgent, WebAgentError,
)


def _durable(gen_owner=1, gen_ctl=1):
    from backend.models.browser_session import (
        BrowserSession, ControllerState, ResourceState)
    return BrowserSession(
        session_id=f"bs-{uuid.uuid4().hex[:10]}",
        tenant_id="owner-u", user_id="owner-u", run_id="r", task_id="t",
        owner_worker_id="w", owner_epoch="ep-1",
        owner_generation=gen_owner, controller_generation=gen_ctl,
        lease_expires_at=int(time.time()) + 600,
        resource_state=ResourceState.READY,
        controller_state=ControllerState.AGENT,
        allowed_origins=["app.example.com"],  # exact-host policy
    )


def test_observation_generation_source_is_not_constant():
    """27.D2 source-level guard: the observation construction must read
    generations from the session/managed session, not hardcode 1."""
    import inspect
    from backend.services import web_agent
    src = inspect.getsource(web_agent)
    assert "owner_generation=1, controller_generation=1" not in src, \
        "fixed generations returned — fencing would compare constants"
    assert "_own_gen" in src and "_ctl_gen" in src


@pytest.mark.asyncio
async def test_validator_unavailable_stops_the_task():
    """27.D3: when the durable session is missing, validation CANNOT
    complete — _validate_dispatch raises ValidationUnavailableError,
    which the run loop treats as a stop (not a refusal-continue)."""
    svc = WebAgent()

    class _Page:
        url = "https://app.example.com/"

        async def evaluate(self, *a, **k):
            return "doc-1"

    snapshot = {"url": "https://app.example.com/", "title": "t",
                "document_id": "doc-1", "elements": [],
                "controller_generation": 1}
    decision = {"action": "click", "index": 1}
    with pytest.raises(ValidationUnavailableError):
        await svc._validate_dispatch(
            _Page(), snapshot, decision,
            {"app.example.com"}, None,  # repo=None → cannot validate
            None, "click", {"index": 1},
            consequential=False, effect_recorded=False)


@pytest.mark.asyncio
async def test_genuine_refusal_still_raises_webagent_error():
    """27.D3: a real policy refusal (stale document) is NOT
    ValidationUnavailable — it remains an actionable refusal."""
    from backend.models.browser_session import (
        BrowserSession, ControllerState, ResourceState)
    svc = WebAgent()

    class _Page:
        url = "https://app.example.com/"

        async def evaluate(self, *a, **k):
            return "doc-CHANGED"   # document changed since observation

    durable = _durable()

    class _Repo:
        async def get_session(self, session_id):
            return durable

    snapshot = {"url": "https://app.example.com/", "title": "t",
                "document_id": "doc-1", "elements": [],
                "controller_generation": 1}
    with pytest.raises(WebAgentError) as exc:
        await svc._validate_dispatch(
            _Page(), snapshot, {"action": "click", "index": 1},
            {"app.example.com"}, _Repo(), durable,
            "click", {"index": 1}, consequential=False,
            effect_recorded=False)
    assert not isinstance(exc.value, ValidationUnavailableError)
    assert "changed" in str(exc.value).lower()


def test_nondefault_port_requires_explicit_grant():
    """§27.4: host-only allow entries admit :443; an explicit
    non-default port must be granted explicitly."""
    from backend.services.web_agent import WebAgent, WebAgentError
    svc = WebAgent
    # Default port passes with host-only entry.
    svc._check_domain("https://app.example.com/x", {"app.example.com"})
    # Non-default port refused without an explicit host:port entry.
    with pytest.raises(WebAgentError) as exc:
        svc._check_domain("https://app.example.com:8443/x",
                          {"app.example.com"})
    assert "8443" in str(exc.value)
    # Explicit host:port grant passes.
    svc._check_domain("https://app.example.com:8443/x",
                      {"app.example.com", "app.example.com:8443"})


def test_managed_session_exposes_generations():
    """27.D2 on the ADOPTED path: ManagedBrowserSession now carries
    public generation access so observation fences work post-adoption."""
    from backend.services.browser_execution import ManagedBrowserSession
    s = ManagedBrowserSession(
        session_id="bs-x", owner_user_id="u", run_id="r", task_id="t",
        allowed_origins=["https://x"], controller_worker_id="w",
        controller_epoch="e", _controller_generation=5)
    assert s.controller_generation == 5
    assert s.owner_generation == 5
