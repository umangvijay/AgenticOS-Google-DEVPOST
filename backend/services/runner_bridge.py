"""Dispatch approved, digest-pinned compute bundles through the container supervisor."""
import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import asdict

from backend.runners.container_runner import ContainerLimits, ContainerRunner
from backend.runners.execution_store import ExecutionStore
from backend.services.isolated_runner import (
    CleanupState, ExecutionRecord, ExecutionRequest, ExecutionState, IsolatedRunner,
    RUNTIME_PROFILES, RunnerUnavailableError, set_runner,
)


class ContainerIsolatedRunner(IsolatedRunner):
    name = "container"

    def __init__(self, *, image, profile_digest, record_store):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("runner image must be an immutable local image digest")
        self.image = image
        self.profile_digest = profile_digest
        self.store = record_store
        self.container = ContainerRunner(image=image, record_store=record_store)
        if hasattr(record_store, "bind_host"):
            record_store.bind_host(self.container._client.info()["ID"])
        self._supervisors = set()

    async def submit_execution(self, request: ExecutionRequest):
        from backend.services.execution_authority import current_authority
        authority = current_authority()
        if authority is not None:
            if request.tenant_id != authority.tenant_id or not await authority.is_current():
                raise RunnerUnavailableError("Workflow attempt no longer authorizes execution")
            request.workflow_run_id = authority.run_id
            request.workflow_task_id = authority.task_id
            request.workflow_attempt = authority.attempt
        if ((request.network_grants and request.policy_profile_id != "read_connector")
                or request.policy_profile_id not in ("pure_compute", "protocol_test", "read_connector")):
            raise RunnerUnavailableError("network grants require an enforcing egress broker")
        if request.runtime_image_digest != self.image:
            raise RunnerUnavailableError("artifact requires a different runtime image")
        if request.deadline_seconds <= 0 or request.resource_limits or request.output_limits:
            raise RunnerUnavailableError("unsupported execution limit override")
        profile = RUNTIME_PROFILES[request.policy_profile_id]
        deadline = time.time() + min(request.deadline_seconds, profile["wall_time_seconds"]) + 60
        try:
            self.store.admit(request.execution_id, asdict(request), deadline)
        except sqlite3.IntegrityError as exc:
            raise RunnerUnavailableError("execution already admitted; reconcile before retrying") from exc
        return ExecutionRecord(request.execution_id, request)

    async def run_to_completion(self, execution_id, script, files, *, protocol_client=None):
        record = await self.get_execution(execution_id)
        if not record or not self.store.transition(execution_id, ["queued"], "running"):
            raise RunnerUnavailableError("execution is no longer queued")
        # Cancellation of an asyncio waiter does not stop its Docker thread.
        # Keep the supervisor alive to collect termination/cleanup evidence.
        supervisor = asyncio.create_task(self._supervise(record, script, files, protocol_client=protocol_client))
        self._supervisors.add(supervisor)
        supervisor.add_done_callback(self._supervisors.discard)
        try:
            return await asyncio.shield(supervisor)
        except asyncio.CancelledError:
            self.store.transition(execution_id, ["running"], "cancel_requested")
            try:
                await asyncio.shield(supervisor)
            finally:
                # Preserve the caller's cancellation; the durable result is
                # independently written by the supervisor, including failures.
                raise asyncio.CancelledError

    async def _supervise(self, record, script, files, *, protocol_client=None):
        execution_id = record.execution_id
        request = record.request
        profile = RUNTIME_PROFILES[request.policy_profile_id]
        limits = ContainerLimits(time_limit_s=min(request.deadline_seconds, profile["wall_time_seconds"]),
                                 pids_limit=profile["max_processes"], memory_mb=profile["memory_mb"],
                                 max_output_bytes=profile["max_output_bytes"],
                                 workspace_mb=profile["workspace_mb"])
        try:
            if request.policy_profile_id == "read_connector" and (not request.network_grants or protocol_client is None):
                raise RunnerUnavailableError("Read connectors require the supervised MCP operation broker")
            options = {"limits": limits, "files": files}
            from backend.services.execution_authority import current_authority
            authority = current_authority()
            if request.workflow_run_id:
                if authority is None or (authority.run_id, authority.task_id, authority.attempt) != (
                        request.workflow_run_id, request.workflow_task_id, request.workflow_attempt):
                    raise RunnerUnavailableError("Workflow execution authority is missing or superseded")
                loop = asyncio.get_running_loop()
                def check_authority():
                    pending = asyncio.run_coroutine_threadsafe(authority.is_current(), loop)
                    try:
                        return pending.result(timeout=3)
                    except Exception:
                        pending.cancel()
                        return False
                options["authority_check"] = check_authority
            if protocol_client is not None:
                options["protocol_client"] = protocol_client
            result = await asyncio.to_thread(self.container.run, execution_id, script, **options)
        except Exception:
            self.store.finish(execution_id, "failed", "orphaned", "supervisor failure")
            raise
        if result.cancelled:
            state = "cancelled"
        elif result.timed_out:
            state = "timed_out"
        elif result.exit_code == 0 and not result.error:
            state = "completed"
        else:
            state = "failed"
        self.store.finish(execution_id, state, result.cleanup_state, result.error or f"exit:{result.exit_code}")
        return await self.get_execution(execution_id)

    async def get_execution(self, execution_id):
        row = self.store.get(execution_id)
        if not row:
            return None
        request = ExecutionRequest(**json.loads(row["request"]))
        result = json.loads(row["result"] or "{}")
        return ExecutionRecord(execution_id, request, state=ExecutionState(row["state"]),
                               cleanup_state=CleanupState(row["cleanup"]),
                               lease_expires_at=row["deadline"], sandbox_identity=result.get("container_id"),
                               started_at=result.get("started_at"), stopped_at=row["stopped"],
                               exit_reason=row["reason"])

    async def request_cancellation(self, execution_id):
        if hasattr(self.store, "cancel_queued"):
            if self.store.cancel_queued(execution_id):
                return True
        elif self.store.transition(execution_id, ["queued"], "cancel_requested"):
            self.store.finish(execution_id, "cancelled", "cleaned", "cancelled before launch")
            return True
        self.store.transition(execution_id, ["running"], "cancel_requested")
        row = self.store.get(execution_id)
        if not row or row["state"] != "cancel_requested":
            return bool(row and row["state"] == "cancelled")
        stopped = await asyncio.to_thread(self.container.cancel, execution_id)
        if stopped:
            return True
        # A different process may own the namespace. Its supervisor polls
        # the durable cancellation intent (every ≤0.25 s) and records
        # confirmed termination. The reconcile wait must cover SIGKILL +
        # durable write on a LOADED host — a fixed 2.5 s budget turned a
        # slow-but-successful cancellation into a false negative when the
        # daemon was busy (full-suite runs hit this on shared hosts).
        for _ in range(100):
            await asyncio.sleep(0.1)
            row = self.store.get(execution_id)
            if row["state"] != "cancel_requested":
                return row["state"] == "cancelled"
        return False

    async def terminate_execution(self, execution_id):
        return await self.request_cancellation(execution_id)

    async def collect_outputs(self, execution_id):
        row = self.store.get(execution_id)
        if not row or not row["result"]:
            return []
        result = json.loads(row["result"])
        return [{"stdout_tail": result.get("stdout", ""), "stderr_tail": result.get("stderr", ""),
                 "exit_code": result.get("exit_code"), "truncated": result.get("truncated", False),
                 "protocol_result": result.get("protocol_result")}]

    async def cleanup_execution(self, execution_id):
        row = self.store.get(execution_id)
        return bool(row and row["cleanup"] == "cleaned")

    async def reconcile_on_restart(self):
        reconciled = 0
        for execution_id in self.store.expired():
            await asyncio.to_thread(self.container.remove_orphans, execution_id)
            self.store.finish(execution_id, "failed", "cleaned", "expired supervisor lease")
            self.store.cleaned(execution_id)
            reconciled += 1
        return reconciled

    async def shutdown(self):
        await asyncio.to_thread(self.container.shutdown)
        if self._supervisors:
            await asyncio.gather(*list(self._supervisors), return_exceptions=True)
        if hasattr(self.store, "close"):
            await asyncio.to_thread(self.store.close)
        self.container._client.close()


async def reconcile_runner_once():
    """Worker housekeeping also reaps expirations after startup."""
    from backend.services.isolated_runner import get_runner
    runner = get_runner()
    if isinstance(runner, ContainerIsolatedRunner):
        return await runner.reconcile_on_restart()
    return 0


async def configure_runner():
    image = os.environ.get("AGENTOS_RUNNER_IMAGE_DIGEST")
    profile = os.environ.get("AGENTOS_RUNNER_PROFILE_DIGEST")
    store_path = os.environ.get("AGENTOS_RUNNER_JOURNAL")
    database_url = os.environ.get("AGENTOS_RUNNER_DATABASE_URL")
    if not any((image, profile, store_path, database_url)):
        set_runner(None)
        return
    if not all((image, profile, store_path or database_url)):
        raise ValueError("runner configuration requires image digest, approved profile digest and journal path")
    if store_path and database_url:
        raise ValueError("Choose one runner journal: PostgreSQL or a local file")
    if database_url:
        from backend.runners.postgres_execution_store import PostgresExecutionStore
        store = await asyncio.to_thread(PostgresExecutionStore, database_url,
            schema=os.environ.get("AGENTOS_RUNNER_DATABASE_SCHEMA", "public"))
    else:
        store = ExecutionStore(store_path)
    import logging
    logger = logging.getLogger("runner_bridge")
    worker_only = os.environ.get("WORKER_ONLY", "0") == "1"
    try:
        runner = ContainerIsolatedRunner(image=image, profile_digest=profile, record_store=store)
        await runner.reconcile_on_restart()
    except Exception as exc:
        if hasattr(store, "close"):
            await asyncio.to_thread(store.close)
        if worker_only:
            # An EXECUTOR without its engine connection must fail fast —
            # admitting work it can never run would strand builds.
            raise
        # An admission-only process (API) boots without engine authority;
        # the configured EXECUTOR owns the actual isolated execution.
        logger.warning("Managed runner configured but the engine connection "
                       "is unavailable in this process (%s); builds are "
                       "admitted here and executed by the configured worker.",
                       type(exc).__name__)
        set_runner(None)
        return None
    set_runner(runner)
    return runner
