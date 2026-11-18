"""Container-isolated runner (§21.8) — the production isolation backend
implementing the runner contract from ``runner_security.py``.

Isolation is enforced by the CONTAINER, not by convention:

- filesystem: the workload runs as a NON-ROOT user in a scratch tmpfs
  workdir; the host filesystem is not mounted (no bundle ⇒ no access;
  a seeded host secret is unreadable — acceptance test 1);
- network: ``network_disabled=True`` — default-deny egress; cloud
  metadata and raw sockets are unreachable (acceptance tests 3–5);
- resources: memory limit (OOM-terminated within boundary), pids limit
  (process spawning bounded), nano_cpus CPU quota, output capped WHILE
  STREAMING (not collected-then-truncated), wall-clock time limit;
- cancellation: ``stop(timeout=0)`` SIGKILLs the container's PID
  namespace — non-cooperative workloads and their descendants are
  terminated by the container runtime (acceptance tests 9–10);
- records: durable — written through the caller-supplied repository
  (sqlite/postgres), never only in memory;
- identity: the image digest + container id are recorded so the exact
  runtime identity of an execution is auditable.

The LOCAL subprocess runner remains for trusted, same-host tooling
(deterministic verification steps) — it is NOT used for untrusted
generated code (§7).
"""
from __future__ import annotations

import io
import json
import os
import logging
import shlex
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

logger = logging.getLogger("container_runner")

# The runner image: alpine + python3 + a non-root `runner` user
# (built by scripts/build_runner_image.sh or the compose runner build).
DEFAULT_IMAGE = "agentos-runner:latest"
MAX_OUTPUT_BYTES = 64_000


@dataclass
class ContainerLimits:
    time_limit_s: int = 20
    workspace_mb: int = 32
    memory_mb: int = 256
    cpu_cores: float = 0.5
    pids_limit: int = 64
    max_output_bytes: int = MAX_OUTPUT_BYTES
    network: bool = False          # default-deny; an explicit allowlist
    allowed_destinations: List[str] = field(default_factory=list)


@dataclass
class ContainerExecutionRecord:
    execution_id: str
    exit_code: Optional[int] = None
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    stdout: str = ""
    stderr: str = ""
    wall_ms: int = 0
    container_id: Optional[str] = None
    image: str = DEFAULT_IMAGE
    image_digest: Optional[str] = None
    error: Optional[str] = None
    started_at: int = 0
    finished_at: Optional[int] = None
    cleanup_state: str = "pending"
    cleanup_error: Optional[str] = None
    protocol_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class ContainerRunner:
    """Docker-backed isolation supervisor. The docker daemon provides
    the kernel-enforced boundaries; this class drives, observes, records,
    and cancels."""

    def __init__(self, *, image: str = DEFAULT_IMAGE,
                 record_store=None):
        from backend.config.settings import settings
        if settings.APP_ENV.lower() == "production" and record_store is None:
            raise ValueError("Production runner requires a durable execution store")
        import docker
        try:
            self._client = docker.from_env()
        except Exception:  # noqa: BLE001 — any from_env failure
            # macOS + colima: the SDK reads DOCKER_HOST or the default
            # socket only — it does not read the docker CLI context, so
            # an otherwise-healthy colima daemon is invisible. Fall back
            # to the standard colima socket paths (Phase 22: the
            # isolation suite skipped all 12 tests without this).
            self._client = None
            for candidate in (
                    os.path.expanduser(
                        "~/.colima/default/docker.sock"),
                    os.path.expanduser(
                        "~/.colima/_lima/colima/docker.sock")):
                if os.path.exists(candidate):
                    self._client = docker.DockerClient(
                        base_url=f"unix://{candidate}")
                    break
            if self._client is None:
                raise
        self._image = image
        self._records: Dict[str, ContainerExecutionRecord] = {}
        self._containers: Dict[str, Any] = {}
        self._record_store = record_store   # optional durable store
        self._image_digest = self._resolve_digest(image)

    def _resolve_digest(self, image: str) -> Optional[str]:
        try:
            info = self._client.images.get(image)
            return info.id
        except Exception:
            return None

    def _durable_write(self, record: ContainerExecutionRecord) -> None:
        if self._record_store is None:
            return
        self._record_store.put(record.execution_id, record.to_dict())

    def run(self, execution_id: str, script: str, *,
            limits: Optional[ContainerLimits] = None,
            files: Optional[Dict[str, str]] = None,
            protocol_client=None, authority_check=None) -> ContainerExecutionRecord:
        """Run `script` (shell) inside a fresh isolated container. The
        container starts with a HOLD command; the input bundle is seeded
        into the tmpfs workdir; the workload then executes as an exec
        INSIDE the container namespace — so cancelling the container
        (stop → SIGKILL) terminates the workload AND every descendant.
        Output is streamed and capped live."""
        limits = limits or ContainerLimits()
        if limits.network or limits.allowed_destinations:
            raise ValueError("Network grants require an enforcing egress broker; this backend only supports network-disabled execution")
        if not self._image_digest:
            raise ValueError("Runner image must be present and resolve to an immutable digest")
        if execution_id in self._records:
            raise ValueError("Execution identity already exists; reconcile its record before retrying")
        if min(limits.time_limit_s, limits.memory_mb, limits.cpu_cores,
               limits.pids_limit, limits.max_output_bytes) <= 0:
            raise ValueError("Runner limits must be positive")
        for name in (files or {}):
            path = PurePosixPath(name)
            if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Input bundle paths must stay inside the workspace")
        record = ContainerExecutionRecord(
            execution_id=execution_id, image=self._image,
            image_digest=self._image_digest,
            started_at=int(time.time()))
        self._records[execution_id] = record
        self._durable_write(record)  # a failed admission write must prevent launch

        cap = limits.max_output_bytes
        out_len = {"n": 0, "truncated": False}
        chunks_out: List[bytes] = []

        def _accept(raw: bytes) -> None:
            room = cap - out_len["n"]
            if room <= 0:
                if raw:
                    out_len["truncated"] = True
                return
            if len(raw) > room:
                out_len["truncated"] = True
                raw = raw[:room]
            out_len["n"] += len(raw)
            chunks_out.append(raw)

        started = time.monotonic()
        workdir = "/work"
        container = None
        finished = threading.Event()
        timed_out = {"flag": False}
        watcher = None
        try:
            if authority_check is not None and not authority_check():
                raise RuntimeError("Workflow execution authority expired before launch")
            # HOLD container: stays alive for the seeding + workload.
            hold_seconds = limits.time_limit_s + 60
            container = self._client.containers.run(
                self._image_digest,
                ["sh", "-c", f"sleep {hold_seconds}"],
                detach=True,
                network_disabled=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=f"{limits.memory_mb}m",
                pids_limit=limits.pids_limit,
                nano_cpus=int(limits.cpu_cores * 1e9),
                user="1000:1000",
                working_dir=workdir,
                tmpfs={workdir: f"size={limits.workspace_mb * 1048576},uid=1000,gid=1000,rw"},
                remove=False,
                labels={"agentos.execution": execution_id})
            record.container_id = container.id[:12]
            self._containers[execution_id] = container
            self._durable_write(record)

            def _timeout_watch():
                deadline = started + limits.time_limit_s
                check_cancel = getattr(self._record_store, "cancellation_requested", None)
                while time.monotonic() < deadline:
                    if finished.wait(min(0.25, max(0, deadline - time.monotonic()))):
                        return
                    if authority_check is not None:
                        try:
                            authorized = authority_check()
                        except Exception:
                            authorized = False
                        if not authorized:
                            self.cancel(execution_id)
                            return
                    if check_cancel:
                        try:
                            if check_cancel(execution_id) and self.cancel(execution_id):
                                return
                        except Exception:
                            # Loss of the durable supervisor must fail closed.
                            if self.cancel(execution_id):
                                return
                timed_out["flag"] = True
                try:
                    container.stop(timeout=0)
                except Exception:
                    logger.warning("deadline termination failed for %s", execution_id)

            watcher = threading.Thread(target=_timeout_watch, daemon=True)
            watcher.start()

            check_cancel = getattr(self._record_store, "cancellation_requested", None)
            if check_cancel and check_cancel(execution_id):
                self.cancel(execution_id)
                raise RuntimeError("execution cancelled before seeding")
            if files:
                self._seed_files(container, files, workdir)

            exec_api = container.client.api
            exec_id = exec_api.exec_create(
                container.id,
                ["sh", "-c", f"cd {workdir} && " + script],
                user="1000:1000", workdir=workdir,
                environment={}, stdin=protocol_client is not None)["Id"]

            if protocol_client is not None:
                from backend.runners.mcp_channel import DockerMCPChannel
                raw = exec_api.exec_start(exec_id, socket=True)
                channel = DockerMCPChannel(raw, max_bytes=cap,
                    deadline=started + limits.time_limit_s)
                try:
                    # The client and its assertions run in the supervisor,
                    # outside the untrusted container and its writable files.
                    record.protocol_result = protocol_client(channel)
                    channel.close_input()
                    stop_deadline = min(started + limits.time_limit_s, time.monotonic() + 5)
                    while exec_api.exec_inspect(exec_id).get("Running") and time.monotonic() < stop_deadline:
                        time.sleep(.05)
                    record.protocol_result["shutdown_observed"] = not exec_api.exec_inspect(exec_id).get("Running")
                finally:
                    # Docker retains the requests response on the raw socket.
                    # Close the HTTP owner first; closing only its file object
                    # leaves HTTPResponse's later flush targeting a closed file.
                    response = getattr(raw, "_response", None)
                    if response is not None:
                        response.close()
                    raw.close()
            else:
                stream = exec_api.exec_start(exec_id, stream=True, demux=True)
                for stdout_chunk, stderr_chunk in stream:
                    if stdout_chunk:
                        _accept(stdout_chunk)
                    if stderr_chunk:
                        _accept(stderr_chunk)
            inspect = exec_api.exec_inspect(exec_id)
            record.exit_code = int(inspect.get("ExitCode", -1)) \
                if inspect.get("ExitCode") is not None else -1
            if timed_out["flag"]:
                record.timed_out = True
                record.exit_code = 137
        except FileNotFoundError as exc:
            record.error = f"container runtime unavailable: {exc}"
            record.exit_code = 125
        except Exception as exc:
            if record.exit_code is None:
                record.error = f"runner failure: {type(exc).__name__}: {exc}"[:300]
                record.exit_code = 125
        finally:
            finished.set()
            if watcher:
                watcher.join(timeout=1)
            if timed_out["flag"]:
                record.timed_out = True
                record.exit_code = 137
            if container is not None:
                try:
                    container.remove(force=True)
                    record.cleanup_state = "cleaned"
                    self._containers.pop(execution_id, None)
                except Exception as exc:
                    record.cleanup_state = "orphaned"
                    record.cleanup_error = f"container cleanup failed: {type(exc).__name__}"
            else:
                record.cleanup_state = "cleaned"
            record.stdout = b"".join(chunks_out).decode("utf-8", errors="ignore")
            record.truncated = out_len["truncated"]
            record.wall_ms = int((time.monotonic() - started) * 1000)
            record.finished_at = int(time.time())
            self._durable_write(record)
        return record

    @staticmethod
    def _seed_files(container, files: Dict[str, str], workdir: str) -> None:
        """Seed the input bundle via exec (docker cp into a tmpfs target
        is unreliable on some runtimes). Files are SMALL input bundles;
        each is base64-decoded into the scratch dir as user 1000."""
        import base64
        import shlex as _shlex
        for name, content in files.items():
            encoded = base64.b64encode(content.encode("utf-8")).decode()
            safe_name = _shlex.quote(name)
            parent = _shlex.quote(str(PurePosixPath(workdir) / PurePosixPath(name).parent))
            result = container.exec_run(
                ["sh", "-c",
                 f"mkdir -p {parent} && printf %s {shlex.quote(encoded)} | base64 -d > "
                 f"{workdir}/{safe_name}"], user="1000:1000")
            if result.exit_code not in (0, None):
                raise RuntimeError(
                    f"failed to seed {name!r} into the execution bundle")

    def cancel(self, execution_id: str) -> bool:
        """REAL cancellation: SIGKILL the container's PID namespace —
        a non-cooperative workload cannot ignore it, and descendants die
        with the namespace."""
        record = self._records.get(execution_id)
        if record is None:
            return False
        container = self._containers.get(execution_id)
        if container is None:
            return False
        try:
            container.stop(timeout=0)
        except Exception:
            try:
                container.kill()
            except Exception:
                return False
        record.cancelled = True
        record.error = "cancelled by caller"
        self._durable_write(record)
        return True

    def remove_orphans(self, execution_id: str) -> None:
        for container in self._client.containers.list(
                all=True, filters={"label": f"agentos.execution={execution_id}"}):
            container.remove(force=True)

    def get_record(self, execution_id: str) -> Optional[ContainerExecutionRecord]:
        return self._records.get(execution_id)

    def shutdown(self) -> None:
        for execution_id in list(self._containers):
            self.cancel(execution_id)


class _DockerfileRunnerImage:
    """Build the runner image once (alpine + python + stdlib only)."""
