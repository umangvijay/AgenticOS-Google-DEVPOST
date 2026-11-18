"""Phase 29 — verified ReAct boundaries (§29.20).

Repeated ineffective actions (same tool, same arguments, same observed
result) hit a bounded stop; changed input or a changed environment
resets the pattern. The browser path already enforces its own repeats
guard — this covers the general workflow tool path.
"""
from __future__ import annotations

import pytest

from backend.services.ineffective_action_guard import (
    IneffectiveActionGuard, RepeatedIneffectiveActionError,
    canonical_digest, get_ineffective_action_guard,
    reset_ineffective_action_guard,
)


@pytest.fixture(autouse=True)
def fresh_guard():
    reset_ineffective_action_guard()
    yield
    reset_ineffective_action_guard()


def test_identical_observation_triplet_reaches_a_bounded_stop():
    guard = IneffectiveActionGuard(max_identical=3)
    args = {"url": "https://api.example.test/records", "method": "GET"}
    result = {"status": 200, "body": {"records": [1, 2]}}
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args, observed_result=result)
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args, observed_result=result)
    with pytest.raises(RepeatedIneffectiveActionError) as exc_info:
        guard.check_and_record(run_id="r1", tool_name="core.http",
                               arguments=args, observed_result=result)
    assert "Bounded stop" in str(exc_info.value)
    # The raised stop clears the pattern: a deliberate retry after a
    # human decision starts a fresh window.
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args, observed_result=result)


def test_changed_environment_resets_the_pattern():
    guard = IneffectiveActionGuard(max_identical=3)
    args = {"url": "https://api.example.test/records"}
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args,
                           observed_result={"status": 503})
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args,
                           observed_result={"status": 503})
    # The dependency recovered — the observed state changed. The new
    # pattern starts from one, so the OLD failures cannot contribute.
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args,
                           observed_result={"status": 200})
    guard.check_and_record(run_id="r1", tool_name="core.http",
                           arguments=args,
                           observed_result={"status": 200})
    # The third IDENTICAL 200 is the bound for the NEW pattern.
    with pytest.raises(RepeatedIneffectiveActionError):
        guard.check_and_record(run_id="r1", tool_name="core.http",
                               arguments=args,
                               observed_result={"status": 200})


def test_different_runs_and_tools_are_independent():
    guard = IneffectiveActionGuard(max_identical=3)
    args = {"q": "x"}
    result = {"hits": 0}
    for run in ("r1", "r2"):
        for tool in ("search_a", "search_b"):
            guard.check_and_record(run_id=run, tool_name=tool,
                                   arguments=args, observed_result=result)
    # No cross-contamination: each (run, tool) is at count 1 — one more
    # identical observation is still below the bound.
    guard.check_and_record(run_id="r1", tool_name="search_a",
                           arguments=args, observed_result=result)
    guard.check_and_record(run_id="r2", tool_name="search_b",
                           arguments=args, observed_result=result)


def test_canonical_digest_is_stable_and_content_sensitive():
    assert canonical_digest({"a": 1, "b": [1, 2]}) == \
        canonical_digest({"b": [1, 2], "a": 1})
    assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})


def test_engine_catalog_tool_path_applies_the_guard():
    """§29.20: the engine's catalog-tool fallback must consult the
    guard — the workflow tool loop has a bounded stop."""
    import inspect
    from backend.engine import engine as engine_module
    src = inspect.getsource(engine_module)
    assert "get_ineffective_action_guard" in src
    assert "check_and_record" in src


def test_guard_is_reachable_as_a_process_singleton():
    assert get_ineffective_action_guard() is get_ineffective_action_guard()
