"""Trusted workflow-attempt authority propagated to isolated child work."""
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

_current = ContextVar("workflow_execution_authority", default=None)


@dataclass(frozen=True)
class ExecutionAuthority:
    repository: object
    run_id: str
    task_id: str
    attempt: int
    tenant_id: str

    async def is_current(self):
        from backend.engine.repo_adapter import maybe_await
        run = await maybe_await(self.repository.get_run(self.run_id))
        if not run or run.get("user_id") != self.tenant_id or run.get("status") != "RUNNING":
            return False
        task = next((t for t in run.get("tasks", []) if t.get("task_id") == self.task_id), None)
        if not task or task.get("status") != "RUNNING" or task.get("attempt") != self.attempt:
            return False
        expiry = task.get("lease_expires_at")
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return isinstance(expiry, datetime) and expiry.tzinfo is not None and expiry > datetime.now(timezone.utc)


def bind(authority):
    return _current.set(authority)


def reset(token):
    _current.reset(token)


def current_authority():
    return _current.get()
