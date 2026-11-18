"""Recovery episode verification write-back (§29.23).

The engine re-verifies a repaired task's output through the Outcome
Verifier, but until now that verdict never reached the durable episode:
VERIFYING→VERIFIED_RECOVERY was unreachable and no production code
produced it, so "recovery closed only after verification" existed as a
state machine with no writer. This module is the writer: after the
engine maps the verifier verdict to the task state, the repaired task's
OPEN episode is walked (CAS transitions, each hop tolerant of a state
already advanced elsewhere) into VERIFIED_RECOVERY on a passing verdict
or RECOVERY_FAILED on a failing one.

A queued repair that never produces a verified result therefore cannot
be reported as a successful recovery (§12.26), and the episode table
gains its first VERIFIED_RECOVERY producers.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The chain the engine walks from a PROPOSED repair to a verdict state.
_PROPOSED_TO_VERIFYING = (
    "VALIDATED", "READY", "EXECUTING", "VERIFYING")


async def resolve_episode_with_verdict(run_id: str, task_id: str,
                                       verdict: str,
                                       evidence_summary: str) -> \
        dict:
    """Write the task's verification verdict back to its open recovery
    episode. Returns a small record of what happened; never raises."""
    from backend.repositories.recovery_episode_repository import (
        get_recovery_episode_repo, RecoveryState,
    )
    repo = get_recovery_episode_repo()
    if repo is None:
        return {"resolved": False, "reason": "episode repo unavailable"}
    try:
        episode = await repo.get_episode_for_task(run_id, task_id)
    except Exception:
        episode = None
    if not episode:
        return {"resolved": False, "reason": "no episode for task"}
    state = str(episode.get("state") or "").upper()
    episode_id = episode["episode_id"]
    if state in ("VERIFIED_RECOVERY", "RECOVERY_FAILED", "ESCALATED",
                 "CANCELLED", "SUPERSEDED"):
        return {"resolved": False, "reason": f"episode already {state}"}

    async def _advance(target: RecoveryState, actor: str,
                       **kwargs) -> bool:
        try:
            await repo.transition(episode_id, target, actor=actor, **kwargs)
            return True
        except Exception as exc:
            # A conflicting CAS means another writer advanced the state;
            # tolerate it and continue the walk from wherever it is now.
            logger.debug("Episode %s transition to %s not applied: %s",
                         episode_id, target.value, exc)
            return False

    # 1) Non-lease hops: advance the proposal chain to READY. The
    #    episode may sit in a later state already — CAS conflicts are
    #    tolerated hop by hop.
    if state == "CLASSIFIED":
        await _advance(RecoveryState.PROPOSED, "system")
        state = "PROPOSED"
    if state == "PROPOSED":
        await _advance(RecoveryState.VALIDATED, "system")
        state = "VALIDATED"
    if state == "VALIDATED":
        await _advance(RecoveryState.READY, "system")
        state = "READY"

    # 2) OWNERSHIP: the verdict transitions are protected — the verifier
    #    must hold the live lease. The recovery worker released
    #    ownership after queueing the repair (§29.23), so this is the
    #    controlled reacquisition path.
    claimed = await repo.claim_episode(
        episode_id, worker_id=f"engine-verifier:{run_id}:{task_id}"[:80])
    if not claimed:
        return {"resolved": False,
                "reason": "episode lease held by another worker"}
    owner_generation = claimed.get("owner_generation")

    # 3) Lease-protected hops into verification, then the verdict.
    if str(claimed.get("state") or "").upper() == "READY":
        await _advance(RecoveryState.EXECUTING, "system",
                       expected_owner_generation=owner_generation,
                       require_live_lease=True)
    await _advance(RecoveryState.VERIFYING, "system",
                   expected_owner_generation=owner_generation,
                   require_live_lease=True)

    # 4) The verdict transition — the only writer of the terminal states.
    try:
        if verdict == "pass":
            await repo.transition(episode_id, RecoveryState.VERIFIED_RECOVERY,
                                  actor="verifier", require_live_lease=True,
                                  evidence_reference=(
                                      f"verified:{evidence_summary}")[:160])
            return {"resolved": True,
                    "terminal": RecoveryState.VERIFIED_RECOVERY.value}
        if verdict in ("fail", "needs_review"):
            await repo.transition(episode_id, RecoveryState.RECOVERY_FAILED,
                                  actor="verifier", require_live_lease=True,
                                  evidence_reference=(
                                      f"unverified:{evidence_summary}")[:160])
            return {"resolved": True,
                    "terminal": RecoveryState.RECOVERY_FAILED.value}
        return {"resolved": False, "reason": f"unhandled verdict {verdict}"}
    except Exception as exc:
        logger.warning("Episode %s verdict transition failed: %s",
                       episode_id, exc)
        return {"resolved": False, "reason": str(exc)[:120]}
