"""
AgentOS — Idempotency Guard

Execution deduplication wrapper for the ToolRouter.
SHA-256 deterministic key: hash(workflow_id + task_id + tool_name + sorted(arguments)).

Usage:
    guard = IdempotencyGuard(idempotency_repo)
    result = await guard.execute_once(workflow_id, task_id, tool_name, args, execute_fn)
"""

import json
import hashlib
import logging
import asyncio
from typing import Optional, Dict, Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class IdempotencyResult:
    """Result from an idempotency-guarded execution."""

    def __init__(
        self,
        executed: bool,
        result: Optional[Dict[str, Any]] = None,
        from_cache: bool = False,
        blocked: bool = False,
        reason: Optional[str] = None,
    ):
        self.executed = executed
        self.result = result
        self.from_cache = from_cache
        self.blocked = blocked
        self.reason = reason


class IdempotencyGuard:
    """
    Deduplicates tool execution; uncertain remote effects require reconciliation.

    Flow:
    1. Compute deterministic key from (workflow_id, task_id, tool_name, arguments)
    2. Attempt atomic INSERT into idempotency_ledger
    3. If INSERT succeeds → we own the lock → execute → commit success/failure
    4. If INSERT fails (key exists):
       - status='completed' → return cached result (zero-cost replay)
       - status='running'   → abort (concurrent execution detected)
       - status='failed'    → return blocked (needs retry with new key)
    """

    def __init__(self, idempotency_repo):
        self.repo = idempotency_repo

    @staticmethod
    def compute_key(
        workflow_id: str, task_id: str, tool_name: str, arguments: Dict[str, Any]
    ) -> str:
        """Compute a deterministic SHA-256 idempotency key."""
        # Sort arguments for deterministic serialization
        args_str = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        raw = f"{workflow_id}:{task_id}:{tool_name}:{args_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def execute_once(
        self,
        workflow_id: str,
        task_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable[..., Awaitable[Dict[str, Any]]],
        retry_safe: bool = False,
    ) -> IdempotencyResult:
        """
        Execute a tool call once per ledger claim.

        Args:
            workflow_id: The workflow this task belongs to
            task_id: The specific task
            tool_name: The MCP tool being called
            arguments: The tool arguments
            execute_fn: The actual function to call (async, returns dict)

        Returns:
            IdempotencyResult with execution outcome
        """
        key = self.compute_key(workflow_id, task_id, tool_name, arguments)

        # Step 1: Attempt to claim the lock
        claimed = await self.repo.atomic_insert(key, workflow_id, task_id)

        if claimed:
            async def quarantine():
                try:
                    await self.repo.commit_uncertain(key)
                except Exception:
                    # Retain the original running lock on a store outage. Never release it for replay.
                    logger.exception("Could not persist uncertain execution %s", key[:12])

            try:
                result = await execute_fn()
            except asyncio.CancelledError:
                await asyncio.shield(quarantine())
                raise
            except Exception as exc:
                from backend.services.auth_challenges import ChallengePause
                from backend.services.approvals_engine import ApprovalRequiredException
                if isinstance(exc, (ChallengePause, ApprovalRequiredException)):
                    await self.repo.commit_pause(key)
                elif retry_safe:
                    await self.repo.commit_failure(key)
                else:
                    await quarantine()
                raise

            # An external success followed by a ledger failure is NOT an execution failure.
            try:
                await self.repo.commit_success(key, json.dumps(result) if result is not None else "null")
            except Exception:
                await quarantine()
                return IdempotencyResult(executed=True, blocked=True,
                    reason="External outcome uncertain: result could not be committed; reconcile before retry")
            return IdempotencyResult(executed=True, result=result)

        else:
            # Key already exists — check status
            record = await self.repo.get_record(key)

            if not record:
                # Shouldn't happen, but handle gracefully
                return IdempotencyResult(
                    executed=False, blocked=True,
                    reason="Idempotency record disappeared unexpectedly"
                )

            status = record.get("status", "unknown")

            if status == "completed":
                # Zero-cost replay — return cached result
                cached = record.get("result_payload", "{}")
                try:
                    result = json.loads(cached) if cached else {}
                except json.JSONDecodeError:
                    result = {}

                logger.info(
                    f"[IDEMPOTENCY] Cache hit (zero-cost replay): {tool_name} "
                    f"(key={key[:12]}...)"
                )
                return IdempotencyResult(
                    executed=False, result=result, from_cache=True
                )

            elif status == "running":
                # Another worker is actively executing this
                logger.warning(
                    f"[IDEMPOTENCY] Concurrent execution blocked: {tool_name} "
                    f"(key={key[:12]}...)"
                )
                return IdempotencyResult(
                    executed=False, blocked=True,
                    reason="Execution is running or interrupted; reconcile an expired execution before retry"
                )

            elif status == "uncertain":
                return IdempotencyResult(executed=False, blocked=True,
                    reason="External outcome uncertain; reconcile with the provider before retry")

            elif status == "failed":
                # Previous attempt failed — needs retry with different key
                logger.info(
                    f"[IDEMPOTENCY] Previous attempt failed: {tool_name} "
                    f"(key={key[:12]}...)"
                )
                return IdempotencyResult(
                    executed=False, blocked=True,
                    reason="Previous execution failed — use the workflow Retry action"
                )

            else:
                return IdempotencyResult(
                    executed=False, blocked=True,
                    reason=f"Unknown idempotency status: {status}"
                )
