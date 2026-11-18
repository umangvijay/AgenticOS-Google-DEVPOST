"""Phase 20 §20.13: Firestore emulator acceptance.

- The anti-production guard is tested DETERMINISTICALLY (no server
  needed): unset env, non-loopback host, and real credentials all
  fail CLOSED.
- When an emulator is reachable, the live acceptance runs: a real
  write/read/update/query/delete contract through the production
  FirestoreDB client, plus the FULL existing parity suites
  (phase1/phase3/phase8-11/command/budget) executed against the
  emulator — every collected test must pass with ZERO skips.
- Without an emulator the live tests report the LOUD block (runnable
  command included) instead of failing silently.
"""
import os
import uuid
from unittest import mock

import pytest

from backend.tests.firestore_emulator_harness import (
    PROJECT_ID,
    block_report,
    emulator_reachable,
    guard,
)


# ── anti-production guard (deterministic, always runs) ─────────────────

def test_guard_refuses_without_emulator_env():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
        result = guard()
        assert not result.ok
        assert "REAL project" in result.reason
        assert "docker run" in result.reason, \
            "the refusal carries the runnable emulator command"


def test_guard_refuses_non_loopback_emulator_host():
    with mock.patch.dict(os.environ,
                         {"FIRESTORE_EMULATOR_HOST": "prod-proxy.internal:8080"},
                         clear=False):
        result = guard()
        assert not result.ok
        assert "loopback" in result.reason


def test_guard_refuses_real_credentials_in_environment(tmp_path):
    cred_file = tmp_path / "prod-creds.json"
    cred_file.write_text('{"type": "service_account"}')
    with mock.patch.dict(os.environ, {
        "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
        "GOOGLE_APPLICATION_CREDENTIALS": str(cred_file),
    }, clear=False):
        result = guard()
        assert not result.ok
        assert "real credentials" in result.reason


def test_guard_accepts_loopback_emulator_without_credentials():
    with mock.patch.dict(os.environ, {
        "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
    }, clear=False):
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        assert guard().ok


# ── live acceptance (emulator-gated, LOUD when blocked) ────────────────

def _require_emulator():
    import os
    # UNSET env = not configured → a LOUD skip (running would require
    # Application Default Credentials = a REAL project — forbidden).
    if not os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip():
        pytest.skip(block_report())
    # Set but misconfigured (non-loopback, real credentials in scope) →
    # the run was REQUESTED and the guard REFUSES it: fail loudly.
    g = guard()
    if not g.ok:
        pytest.fail(f"§20.13 guard refused the run: {g.reason}")
    if not emulator_reachable():
        pytest.skip(block_report())


@pytest.mark.asyncio
async def test_emulator_smoke_contract_write_read_query_delete():
    _require_emulator()
    from backend.tests.firestore_emulator_harness import smoke_contract
    checks = await smoke_contract()
    assert checks == ["write", "read", "update", "query", "delete"]


def test_parity_suites_pass_against_emulator_with_zero_skips():
    """THE §20.13 acceptance: the full existing parity suites run
    against the emulator. Skips are FORBIDDEN here — a skip inside the
    guarded run would hide untested behavior, so the output must show
    'passed' with no 'skipped'."""
    _require_emulator()
    from backend.tests.firestore_emulator_harness import run_parity_suites
    code, tail = run_parity_suites(timeout=900)
    assert code == 0, f"parity suites failed against the emulator:\n{tail}"
    assert "skipped" not in tail.lower() or "no tests ran" in tail.lower(), \
        f"skips inside a guarded emulator run are forbidden:\n{tail}"
    assert "passed" in tail
