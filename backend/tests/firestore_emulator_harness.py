"""Firestore emulator acceptance harness (Phase 20 §20.13).

Local-first contract: the emulator is the ONLY acceptable target for
harness runs. Three anti-production guards, all fail-CLOSED:

1. FIRESTORE_EMULATOR_HOST must be set — with no emulator configured the
   google client would silently fall through to Application Default
   Credentials, i.e. a REAL project. Unset ⇒ refuse.
2. The emulator host must be loopback (127.0.0.1/localhost/::1). A
   non-loopback "emulator" could be a proxy to real infrastructure ⇒ refuse.
3. GOOGLE_APPLICATION_CREDENTIALS must NOT point at an existing file —
   real credentials in the environment mean a real project is reachable ⇒
   refuse (the emulator needs no credentials).

When no emulator is reachable, ``block_report`` returns a LOUD, explicit
block (runnable docker command included) instead of a silent skip.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

EMULATOR_HOST_DEFAULT = "127.0.0.1:8080"
PROJECT_ID = "agentos-local-test"  # dedicated non-production project id
RUNNABLE_CMD = ("docker run -d --name agentos-fs-emulator -p 8080:8080 "
                "-e FIRESTORE_PROJECT_ID=agentos-local-test "
                "gcr.io/google.com/cloudsdktool/cloud-sdk:emulators "
                "gcloud emulators firestore start --host-port=0.0.0.0:8080")
# or: docker compose --profile emulators up firestore-emulator

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# Parity suites that must pass against the emulator (the SAME contracts
# that pass on SQLite locally).
PARITY_SUITES = [
    "backend/tests/test_phase1_firestore_parity.py",
    "backend/tests/test_phase3_firestore_parity.py",
    "backend/tests/test_phase8_11_firestore_parity.py",
    "backend/tests/test_command_firestore_parity.py",
    "backend/tests/test_budget_firestore_parity.py",
    "backend/tests/test_phase26_feedback_firestore.py",
    "backend/tests/test_phase26_firestore_wait.py",
]


@dataclass
class GuardResult:
    ok: bool
    reason: str
    emulator_host: Optional[str] = None


def guard() -> GuardResult:
    """Anti-production guard. Checks, in order: emulator env set, host is
    loopback, no real application credentials in the environment."""
    host = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()
    if not host:
        return GuardResult(
            False,
            "FIRESTORE_EMULATOR_HOST is not set — without it the Firestore "
            "client would authenticate with Application Default Credentials "
            "against a REAL project. The harness refuses to run unguarded. "
            "Set it (e.g. FIRESTORE_EMULATOR_HOST=127.0.0.1:8080) or start "
            f"the emulator: {RUNNABLE_CMD}")
    hostname = host.split(":")[0].strip("[]")
    if hostname not in LOOPBACK_HOSTS:
        return GuardResult(
            False,
            f"FIRESTORE_EMULATOR_HOST={host} is not loopback — the harness "
            "only talks to a local emulator, never a remote endpoint that "
            "could proxy to production")
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if creds and os.path.exists(creds):
        return GuardResult(
            False,
            f"GOOGLE_APPLICATION_CREDENTIALS={creds} points at a real "
            "credentials file — running the harness with production "
            "credentials in scope is forbidden; unset it for emulator runs")
    return GuardResult(True, "emulator guard satisfied", emulator_host=host)


def emulator_reachable(timeout: float = 5.0) -> bool:
    host = os.environ.get("FIRESTORE_EMULATOR_HOST",
                          EMULATOR_HOST_DEFAULT).strip()
    hostname, _, port = host.partition(":")
    try:
        with socket.create_connection((hostname, int(port or 8080)),
                                      timeout=timeout):
            return True
    except OSError:
        return False


def block_report() -> str:
    """LOUD block text — surfaced in skips, never swallowed."""
    return ("BLOCKED (Firestore emulator acceptance): no emulator on "
            f"{os.environ.get('FIRESTORE_EMULATOR_HOST', EMULATOR_HOST_DEFAULT)}. "
            f"Start one with: {RUNNABLE_CMD}  — or —  "
            "docker compose --profile emulators up firestore-emulator; "
            "then run: FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 "
            f"GOOGLE_CLOUD_PROJECT={PROJECT_ID} pytest "
            "backend/tests/test_phase20_firestore_emulator.py -q")


async def smoke_contract() -> List[str]:
    """Real write/read/query/delete through the FirestoreDB client used
    by every repository. Returns the list of verified operations."""
    import uuid
    from backend.repositories.firestore.database import FirestoreDB

    FirestoreDB._instance = None
    client = await FirestoreDB.get_client()
    checks: List[str] = []
    run_id = f"harness-{uuid.uuid4().hex[:8]}"
    try:
        doc = client.collection("harness_smoke").document(run_id)
        await doc.set({"run_id": run_id, "state": "pending", "n": 1})
        checks.append("write")
        snap = await doc.get()
        assert snap.exists and snap.to_dict()["state"] == "pending"
        checks.append("read")
        await doc.update({"state": "running", "n": 2})
        snap = await doc.get()
        assert snap.to_dict()["state"] == "running"
        checks.append("update")
        query = client.collection("harness_smoke").where(
            "state", "==", "running")
        found = [d.id async for d in query.stream() if d.id == run_id]
        assert found == [run_id]
        checks.append("query")
        await doc.delete()
        snap = await doc.get()
        assert not snap.exists
        checks.append("delete")
    finally:
        client.close()
        FirestoreDB._instance = None
    return checks


def run_parity_suites(timeout: int = 600) -> Tuple[int, str]:
    """Run the existing Firestore parity suites against the emulator in
    a subprocess with the guard env. Returns (exit_code, tail_output)."""
    env = dict(os.environ)
    env["FIRESTORE_EMULATOR_HOST"] = env.get(
        "FIRESTORE_EMULATOR_HOST", "").strip() or EMULATOR_HOST_DEFAULT
    env["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID
    # The guard refuses real credentials — ensure none leak in.
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    repo_root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *PARITY_SUITES, "-q",
         "--no-header", "-x"],
        env=env, capture_output=True, text=True, timeout=timeout,
        cwd=repo_root)
    tail = (proc.stdout or "")[-3000:]
    return proc.returncode, tail
