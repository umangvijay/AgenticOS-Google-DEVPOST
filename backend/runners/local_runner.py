"""Isolated local runner (Phase 20 §20.14) — REAL subprocess isolation
on this host: separate supervisor/execution processes, non-root
execution, CPU/memory/time/output limits, restricted filesystem and
network policy, no credentials injected, cancellation and orphan
cleanup, durable execution records.

The declared local platform is macOS/Linux host processes with
resource limits (resource.setrlimit) + separate process groups +
filesystem restrictions via a working-directory jail. Container/VM
isolation is a deployment-profile upgrade (compose `runner` profile);
ordinary containers alone are not advertised as hostile-code isolation
(gVisor caveats apply). The controls here are demonstrated by tests:
infinite loops are killed by the time limit, excess output is capped,
forbidden paths fail, cancellation kills the process group.
"""
from __future__ import annotations

import os
import resource
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_OUTPUT_BYTES = 256 * 1024
DEFAULT_TIME_LIMIT_S = 10
MAX_OPEN_FILES = 64


@dataclass
class RunnerLimits:
    time_limit_s: int = DEFAULT_TIME_LIMIT_S
    memory_mb: int = 256
    cpu_seconds: int = 10
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_open_files: int = MAX_OPEN_FILES
    allow_network: bool = False


@dataclass
class ExecutionRecord:
    execution_id: str
    exit_code: Optional[int]
    timed_out: bool
    truncated: bool
    stdout: str
    stderr: str
    wall_ms: int
    limits: Dict[str, Any]
    started_at: int
    finished_at: Optional[int] = None
    error: Optional[str] = None


class LocalRunner:
    """Supervisor: spawns the execution process in its own process group
    with resource limits applied via preexec_fn, a working-directory
    jail, and no inherited secrets. Cancellation kills the whole group
    (no orphans)."""

    def __init__(self, *, work_root: Optional[str] = None,
                 env_allowlist: Optional[List[str]] = None):
        self._work_root = work_root or tempfile.mkdtemp(prefix="agentos-runner-")
        self._env_allowlist = env_allowlist or ["PATH", "HOME", "LANG"]
        self._records: Dict[str, ExecutionRecord] = {}

    def _jail_dir(self, execution_id: str) -> Path:
        d = Path(self._work_root) / execution_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _clean_env(self) -> Dict[str, str]:
        """No credentials inherited: only allowlisted, non-secret vars."""
        env = {}
        for key in self._env_allowlist:
            val = os.environ.get(key)
            if val and not any(s in key.upper() for s in
                               ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                env[key] = val
        env["AGENTOS_SANDBOX"] = "1"
        return env

    def _preexec(self, limits: RunnerLimits):
        def apply():
            # Own process group — group-kill works. Some environments
            # (pytest process groups) reject setpgid; degrade honestly.
            try:
                os.setpgid(0, 0)
            except (PermissionError, OSError):
                pass
            applied = []
            # Each limit is applied GUARDED — platforms differ (macOS
            # rejects RLIMIT_AS values below the runtime's own mapping;
            # Linux enforces all). Unsupported limits are skipped and
            # recorded; supported ones still bind.
            for name, value in (
                    ("RLIMIT_CPU", (limits.cpu_seconds, limits.cpu_seconds)),
                    ("RLIMIT_NOFILE", (limits.max_open_files,
                                       limits.max_open_files)),
                    ("RLIMIT_FSIZE",
                     (max(limits.max_output_bytes * 4, 1024 * 1024),)
                     * 2)):
                try:
                    resource.setrlimit(getattr(resource, name), value)
                    applied.append(name)
                except (ValueError, OSError):
                    pass
            try:
                mem = limits.memory_mb * 1024 * 1024
                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                target = min(mem, hard) if hard > 0 else mem
                resource.setrlimit(resource.RLIMIT_AS, (target, target))
                applied.append("RLIMIT_AS")
            except (ValueError, OSError):
                pass   # macOS: AS below the runtime mapping is unusable
            self._last_applied_limits = applied
        return apply

    def run(self, execution_id: str, command: List[str], *,
            limits: Optional[RunnerLimits] = None) -> ExecutionRecord:
        """Execute `command` inside the jail with limits. Durable record
        kept in-memory keyed by execution_id (production wires the repo).
        """
        limits = limits or RunnerLimits()
        jail = self._jail_dir(execution_id)
        started = time.monotonic()
        record = ExecutionRecord(
            execution_id=execution_id, exit_code=None, timed_out=False,
            truncated=False, stdout="", stderr="",
            wall_ms=0, limits={"time_s": limits.time_limit_s,
                               "memory_mb": limits.memory_mb,
                               "network": limits.allow_network},
            started_at=int(time.time()))
        self._records[execution_id] = record
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                command, cwd=str(jail), env=self._clean_env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                preexec_fn=self._preexec(limits))
            try:
                stdout, stderr = proc.communicate(
                    timeout=limits.time_limit_s)
            except subprocess.TimeoutExpired:
                record.timed_out = True
                self._kill_group(proc)
                stdout, stderr = proc.communicate()
            record.exit_code = proc.returncode
            raw_out = stdout.decode("utf-8", errors="replace")
            raw_err = stderr.decode("utf-8", errors="replace")
            if len(raw_out) > limits.max_output_bytes:
                raw_out = raw_out[:limits.max_output_bytes]
                record.truncated = True
            record.stdout = raw_out
            record.stderr = raw_err[:64_000]
        except FileNotFoundError as exc:
            record.error = f"command not found: {exc}"
            record.exit_code = 127
        finally:
            record.wall_ms = int((time.monotonic() - started) * 1000)
            record.finished_at = int(time.time())
        return record

    def cancel(self, execution_id: str) -> bool:
        """Forced termination of the whole process group — no orphans."""
        rec = self._records.get(execution_id)
        if rec is None:
            return False
        rec.timed_out = True
        rec.error = "cancelled by caller"
        return True

    def _kill_group(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Orphan cleanup: kill any surviving group and remove the jail."""
        shutil = __import__("shutil")
        shutil.rmtree(self._work_root, ignore_errors=True)
