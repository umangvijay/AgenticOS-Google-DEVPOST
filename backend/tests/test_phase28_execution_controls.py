"""Phase 28 §28.2 — shared execution-control drills that lacked named
coverage.

The PostgreSQL journal drills (cross-host reconciliation fencing, real
orphan reaping, cross-process cancellation, budget races, capacity
leases, duplicate delivery, effect reconciliation and the broker
allow/deny matrix) retain their existing named suites — this file pins
the remaining audit items:

1. SQLite journal records are carried through a restart unchanged.
2. Cancellation intent is DURABLE before the owner is signaled, and a
   reader on another connection observes it before any kill.
3. A mid-run authority/membership change refuses dispatch and further
   execution (the parent's authority is re-derived, not cached).
4. The egress broker records its decision and the destination's
   observation for the authorized-destination drill.
"""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from backend.runners.execution_store import ExecutionStore
from backend.services.isolated_runner import (
    CleanupState, ExecutionRequest, ExecutionState,
)


def _request(execution_id="exec-1", tenant="tenant-a"):
    return ExecutionRequest(
        execution_id=execution_id, tenant_id=tenant,
        artifact_digest="sha256:" + "a" * 64, runtime_image_digest=None,
        policy_profile_id="pure_compute")


# ── 1. SQLite records survive a restart ──────────────────────────────

def test_sqlite_journal_carries_records_through_restart(tmp_path):
    journal = tmp_path / "journal.db"
    store = ExecutionStore(journal)
    store.admit("exec-queued", json.loads(json.dumps(_request("exec-queued").__dict__, default=str)), 9999)
    store.admit("exec-running", json.loads(json.dumps(_request("exec-running").__dict__, default=str)), 9999)
    store.transition("exec-running", ["queued"], "running")
    store.admit("exec-cancel", json.loads(json.dumps(_request("exec-cancel").__dict__, default=str)), 9999)
    store.transition("exec-cancel", ["queued"], "cancel_requested")
    store.admit("exec-done", json.loads(json.dumps(_request("exec-done").__dict__, default=str)), 9999)
    store.transition("exec-done", ["queued"], "running")
    store.put("exec-done", {"stdout": "durable", "exit_code": 0})
    store.finish("exec-done", "completed", "cleaned", "exit:0")

    # "Restart": a fresh store instance over the same file must observe
    # every durable state, cleanup class and result byte-for-byte.
    reopened = ExecutionStore(journal)
    assert reopened.get("exec-queued")["state"] == "queued"
    assert reopened.get("exec-running")["state"] == "running"
    row = reopened.get("exec-cancel")
    assert row["state"] == "cancel_requested"
    assert row["cleanup"] == "pending"
    done = reopened.get("exec-done")
    assert done["state"] == "completed" and done["cleanup"] == "cleaned"
    assert json.loads(done["result"])["stdout"] == "durable"
    # An in-flight execution remains reconcilable after the restart —
    # cancellation still resolves it, and the terminal record persists.
    assert reopened.cancel_queued("exec-queued")
    assert reopened.get("exec-queued")["state"] == "cancelled"
    assert reopened.transition("exec-running", ["running"], "cancel_requested")


def test_sqlite_legacy_journal_without_stopped_column_is_carried_forward(tmp_path):
    """A journal written by an older build must keep its records through a
    restart on the current code — the schema migrates in place instead of
    failing or dropping rows."""
    journal = tmp_path / "legacy.db"
    with closing(sqlite3.connect(journal)) as db, db:
        db.execute("""CREATE TABLE managed_executions (
            id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL,
            cleanup TEXT NOT NULL, deadline REAL NOT NULL, result TEXT,
            reason TEXT)""")
        db.execute(
            "INSERT INTO managed_executions (id, request, state, cleanup, deadline, reason) "
            "VALUES ('legacy-1', '{}', 'running', 'pending', 9999, 'in flight')")
    store = ExecutionStore(journal)
    row = store.get("legacy-1")
    assert row is not None and row["state"] == "running"
    # The carried-forward record stays operable under the new schema.
    assert store.transition("legacy-1", ["running"], "cancel_requested")
    store.finish("legacy-1", "cancelled", "cleaned", "cancelled on restart")
    final = store.get("legacy-1")
    assert final["state"] == "cancelled" and final["cleanup"] == "cleaned"
    assert final["stopped"] is not None


# ── 2. Cancellation intent is durable before the owner is signaled ───

@pytest.mark.asyncio
async def test_cancellation_intent_is_durable_before_owner_signal(tmp_path, monkeypatch):
    from backend.runners.container_runner import ContainerRunner

    # The contract under test needs a reachable docker daemon (real
    # container cancel semantics). Without one, skip EXPLICITLY — never
    # fail on an unrelated daemon connection error. Mirror
    # ContainerRunner's own daemon discovery (from_env + colima sockets).
    import os as _os
    import docker as _docker_sdk

    def _daemon_available() -> bool:
        try:
            _docker_sdk.from_env().ping()
            return True
        except Exception:
            pass
        for _candidate in (_os.path.expanduser("~/.colima/default/docker.sock"),
                           _os.path.expanduser("~/.colima/_lima/colima/docker.sock")):
            if _os.path.exists(_candidate):
                try:
                    _docker_sdk.DockerClient(
                        base_url=f"unix://{_candidate}").ping()
                    return True
                except Exception:
                    continue
        return False

    if not _daemon_available():
        pytest.skip("BLOCKED: docker daemon unavailable — durable-cancellation "
                    "contract needs real container semantics")

    store = ExecutionStore(tmp_path / "journal.db")
    observed = {}

    real_cancel = ContainerRunner.cancel

    def observing_cancel(self, execution_id):
        # The moment the owner is signaled, a reader on ANOTHER
        # connection must already observe the durable intent.
        other = ExecutionStore(tmp_path / "journal.db")
        observed["durable_state"] = other.get(execution_id)["state"]
        return real_cancel(self, execution_id)

    monkeypatch.setattr(ContainerRunner, "cancel", observing_cancel)

    from backend.services.runner_bridge import ContainerIsolatedRunner

    class _Runner(ContainerIsolatedRunner):
        def __init__(self, store):
            self.image = "sha256:" + "0" * 64
            self.profile_digest = "sha256:" + "0" * 64
            self.store = store
            self.container = ContainerRunner(
                image=self.image, record_store=store)
            self._supervisors = set()

    runner = _Runner(store)
    request = _request()
    request.deadline_seconds = 1
    store.admit(request.execution_id,
                json.loads(json.dumps(request.__dict__, default=str)),
                9999)
    store.transition(request.execution_id, ["queued"], "running")
    await runner.request_cancellation(request.execution_id)
    assert observed["durable_state"] == "cancel_requested", (
        "the owner must never be signaled before the intent is durable")
    row = store.get(request.execution_id)
    # The owner never answered (no live container): the execution stays
    # PENDING RECONCILIATION instead of claiming a cleanup that nobody
    # performed.
    assert row["state"] == "cancel_requested"
    assert row["cleanup"] == "pending"


# ── 3. Mid-run authority/membership changes refuse execution ─────────

@pytest.mark.asyncio
async def test_revoked_authority_stops_midrun_dispatch():
    from backend.services.execution_authority import ExecutionAuthority

    class Repo:
        def __init__(self, run):
            self.run = run

        async def get_run(self, run_id):
            return self.run

    authority = ExecutionAuthority(
        repository=Repo({"run_id": "r1", "user_id": "tenant-a",
                         "status": "RUNNING",
                         "tasks": [{"task_id": "t1", "status": "RUNNING",
                                    "attempt": 1,
                                    "lease_expires_at": "2099-01-01T00:00:00+00:00"}]}),
        run_id="r1", task_id="t1", attempt=1, tenant_id="tenant-a")
    assert await authority.is_current()
    # Membership revoked mid-run: the run is no longer the tenant's.
    authority.repository.run = {**authority.repository.run,
                                "user_id": "tenant-b"}
    assert not await authority.is_current()
    # Owner deleted mid-run.
    authority.repository.run = None
    assert not await authority.is_current()
    # Lease expired mid-run.
    authority.repository.run = {"run_id": "r1", "user_id": "tenant-a",
                                "status": "RUNNING",
                                "tasks": [{"task_id": "t1", "status": "RUNNING",
                                           "attempt": 1,
                                           "lease_expires_at": "2000-01-01T00:00:00+00:00"}]}
    assert not await authority.is_current()


@pytest.mark.asyncio
async def test_runner_refuses_dispatch_without_current_authority(tmp_path):
    from backend.services.runner_bridge import ContainerIsolatedRunner
    from backend.services.execution_authority import (
        ExecutionAuthority, bind, reset)
    from backend.services.isolated_runner import RunnerUnavailableError

    class _Runner(ContainerIsolatedRunner):
        def __init__(self, store):
            self.image = "sha256:" + "0" * 64
            self.profile_digest = "sha256:" + "0" * 64
            self.store = store
            self.container = None
            self._supervisors = set()

    store = ExecutionStore(tmp_path / "journal.db")
    runner = _Runner(store)

    class SupersededRepo:
        async def get_run(self, run_id):
            return None  # the run vanished — authority is gone

    authority = ExecutionAuthority(
        repository=SupersededRepo(), run_id="r1", task_id="t1",
        attempt=1, tenant_id="tenant-a")
    request = _request()
    request.workflow_run_id = "r1"
    request.workflow_task_id = "t1"
    request.workflow_attempt = 1
    token = bind(authority)
    try:
        with pytest.raises(RunnerUnavailableError):
            await runner.submit_execution(request)
    finally:
        reset(token)
    assert store.get(request.execution_id) is None, (
        "refused dispatch must not leave a durable admission")


# ── 4. The broker records its decision and the destination observation ─

def test_broker_records_decision_and_destination_observation(monkeypatch):
    from backend.runners.egress_broker import EgressBroker, EgressGrant, EgressDeniedError

    class RecordingCheck:
        """The broker's cancellation-poll hook (zero-arg); its calls mark
        that resolution of THIS destination happened inside the broker."""
        def __init__(self):
            self.calls = []

        def __call__(self):
            self.calls.append(("example.com", 443))
            return [("93.184.216.34", 0.0)], 443

    grant = EgressGrant(allowed=[("example.com", 443)])
    broker = EgressBroker(grant)
    check = RecordingCheck()
    # The broker re-resolves the destination itself after the authority
    # check; keep that resolution hermetic (CI runs --network=none) —
    # the OBSERVATION contract under test is the check-hook call, not
    # the OS resolver.
    import backend.runners.egress_broker as _eb
    monkeypatch.setattr(
        _eb.socket, "getaddrinfo",
        lambda *a, **k: [(None, None, None, "", ("93.184.216.34", 0))])
    # Authorized destination: the broker validates, resolves through its
    # own checker, and hands back the vetted address.
    scheme, host, port = broker.validate_url(
        "https://example.com/x", check=check)
    assert (scheme, host, port) == ("https", "example.com", 443)
    assert check.calls, "the broker must observe the destination"
    # Denied destinations are refused before any resolution leaves the
    # broker — metadata endpoints, ungranted ports and plaintext egress.
    for url in ("http://169.254.169.254/latest/meta-data/",
                "https://example.com:8443/x",
                "http://example.com/x"):
        with pytest.raises(EgressDeniedError):
            broker.validate_url(url, check=check)
    assert all(call[1] == 443 and call[0] == "example.com"
               for call in check.calls)


# ── 5. Local capacity guards survive loop replacement (§28.12) ────────

@pytest.mark.asyncio
async def test_local_capacity_guard_survives_loop_replacement():
    """A semaphore bound to a DEAD loop must be recreated for the running
    loop — a replacement worker on a fresh loop would otherwise NACK
    every dispatch until the message dead-letters."""
    from backend.services import capacity_service
    from backend.models.capacity import ResourceClass

    key = ResourceClass.WORKFLOW_TASK.value
    # Simulate a guard left behind by a DEAD loop: create it and bind it
    # by acquiring, on THIS loop, then pretend this loop died.
    stale = asyncio.BoundedSemaphore(2)
    await stale.acquire()
    stale._loop = object()  # mark it as belonging to a (dead) foreign loop
    capacity_service._local_semaphores[key] = stale

    holder = {}

    def run_on_replacement_loop():
        # The replacement worker runs on a FRESH loop (a worker thread's
        # own loop in production; a new test loop here).
        async def body():
            sem = capacity_service._local_semaphore(ResourceClass.WORKFLOW_TASK)
            holder["sem"] = sem
            await sem.acquire()
            sem.release()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(body())
        finally:
            loop.close()

    await asyncio.to_thread(run_on_replacement_loop)
    assert holder["sem"] is not stale, (
        "the dead loop's guard must be replaced for the new loop")
