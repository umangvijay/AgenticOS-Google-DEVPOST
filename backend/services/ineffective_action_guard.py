"""Bounded-stop for repeated ineffective actions (§29.20).

The browser path already refuses repeated identical actions
(`web_agent` ineffective_repeats); the GENERAL workflow path had no
equivalent — an orchestrator or catalog-tool loop that calls the same
tool with the same arguments and receives the same observed result
burns steps, time and spend without changing the world.

The guard keys on (run, tool, canonical arguments digest, observed
result digest): target + action + observed state. The Nth identical
triple raises `RepeatedIneffectiveActionError`, which the task treats
as a bounded stop — a durable, actionable failure, not a silent loop.
Different arguments or a different observed result reset the pattern
(the environment genuinely changed).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_IDENTICAL_REPEATS = 3


class RepeatedIneffectiveActionError(RuntimeError):
    """A bounded stop: the same action produced the same observed result
    too many times within one run."""


@dataclass
class _Pattern:
    count: int


def canonical_digest(payload) -> str:
    """A stable digest of arguments or an observed result — the same
    semantic payload always hashes the same; a changed payload hashes
    differently (versioned canonicalization)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()).hexdigest()


class IneffectiveActionGuard:
    """Per-process observation of (run, tool, args, result) repetition.
    The bounded stop is durable through the task failure it raises —
    the error text, the step record and the run event carry the reason."""

    def __init__(self, max_identical: int = MAX_IDENTICAL_REPEATS):
        self._max = max(2, int(max_identical))
        self._patterns: dict[tuple, _Pattern] = {}
        self._lock = threading.Lock()

    def check_and_record(self, *, run_id: str, tool_name: str,
                         arguments, observed_result) -> None:
        """Record one completed tool observation; raise when the same
        triple has now been observed `max_identical` times."""
        args_digest = canonical_digest(arguments)
        result_digest = canonical_digest(observed_result)
        key = (run_id, str(tool_name), args_digest, result_digest)
        with self._lock:
            pattern = self._patterns.get(key)
            if pattern is None:
                self._patterns[key] = _Pattern(count=1)
                # Bound the guard's memory: a long-lived worker must not
                # accumulate every observation forever.
                if len(self._patterns) > 2048:
                    for stale in list(self._patterns)[:1024]:
                        self._patterns.pop(stale, None)
                return
            pattern.count += 1
            count = pattern.count
        if count >= self._max:
            with self._lock:
                self._patterns.pop(key, None)
            raise RepeatedIneffectiveActionError(
                f"Bounded stop: {tool_name} was called with identical "
                f"arguments and returned an identical result {count} "
                f"times in run {run_id[:8]} — the action is ineffective "
                "without changed input or a changed environment")


_guard = IneffectiveActionGuard()


def get_ineffective_action_guard() -> IneffectiveActionGuard:
    return _guard


def reset_ineffective_action_guard(max_identical: int =
                                   MAX_IDENTICAL_REPEATS) -> None:
    global _guard
    _guard = IneffectiveActionGuard(max_identical)
