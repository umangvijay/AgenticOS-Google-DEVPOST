"""Durable recovery episodes (Phase 11 §11.2/§11.3/§11.7/§11.8).

One recovery episode per failed task with the full §11.2 field set: identity,
original contract, failure classification, proposal, policy, ownership,
limits, accounting, effects, and outcome. State machine (§11.3) with atomic
CAS transitions and ownership generation (§11.7).
"""
from __future__ import annotations

import json
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now


def new_episode_id() -> str:
    return f"ep_{uuid.uuid4().hex[:24]}"


class RecoveryState(str, Enum):
    DETECTED = "DETECTED"
    CLASSIFIED = "CLASSIFIED"
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    READY = "READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    VERIFIED_RECOVERY = "VERIFIED_RECOVERY"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL_RECOVERY_STATES = frozenset({
    RecoveryState.VERIFIED_RECOVERY, RecoveryState.RECOVERY_FAILED,
    RecoveryState.ESCALATED, RecoveryState.CANCELLED,
    RecoveryState.SUPERSEDED,
})

ALLOWED_RECOVERY_TRANSITIONS: Dict[RecoveryState, Dict[RecoveryState, set]] = {
    RecoveryState.DETECTED: {
        RecoveryState.CLASSIFIED: {"system"},
        # §12.5: a deadline expiry can precede classification — the
        # persistent absolute deadline escalates from DETECTED directly.
        RecoveryState.ESCALATED: {"system"},
        RecoveryState.CANCELLED: {"operator", "system"},
        RecoveryState.SUPERSEDED: {"system"},
    },
    RecoveryState.CLASSIFIED: {
        RecoveryState.PROPOSED: {"agent", "system"},
        RecoveryState.ESCALATED: {"system", "operator"},
        RecoveryState.CANCELLED: {"operator", "system"},
        RecoveryState.SUPERSEDED: {"system"},
    },
    RecoveryState.PROPOSED: {
        RecoveryState.VALIDATED: {"system"},
        RecoveryState.ESCALATED: {"system", "operator"},
        RecoveryState.CANCELLED: {"operator", "system"},
        RecoveryState.SUPERSEDED: {"system"},
    },
    RecoveryState.VALIDATED: {
        RecoveryState.READY: {"system"},
        RecoveryState.ESCALATED: {"system"},
        RecoveryState.CANCELLED: {"operator", "system"},
        RecoveryState.SUPERSEDED: {"system"},
    },
    RecoveryState.READY: {
        RecoveryState.EXECUTING: {"system", "agent"},
        RecoveryState.CANCELLED: {"operator", "system"},
        RecoveryState.SUPERSEDED: {"system"},
    },
    RecoveryState.EXECUTING: {
        RecoveryState.VERIFYING: {"system", "agent"},
        RecoveryState.RECOVERY_FAILED: {"system"},
        RecoveryState.CANCELLED: {"operator", "system"},
    },
    RecoveryState.VERIFYING: {
        RecoveryState.VERIFIED_RECOVERY: {"verifier", "system"},
        RecoveryState.RECOVERY_FAILED: {"verifier", "system"},
        RecoveryState.ESCALATED: {"system"},
        RecoveryState.CANCELLED: {"operator", "system"},
    },
    # Terminal
    RecoveryState.VERIFIED_RECOVERY: {},
    RecoveryState.RECOVERY_FAILED: {},
    RecoveryState.ESCALATED: {},
    RecoveryState.CANCELLED: {},
    RecoveryState.SUPERSEDED: {},
}


class RecoveryTransitionError(Exception):
    def __init__(self, message: str, current: RecoveryState, state_version: int):
        super().__init__(message)
        self.current = current
        self.state_version = state_version


def recovery_transition_valid(current: RecoveryState, target: RecoveryState,
                              actor: str) -> bool:
    allowed = ALLOWED_RECOVERY_TRANSITIONS.get(current, {})
    return actor in allowed.get(target, set())


# §12.5: the states whose transitions emit durable outbox intents —
# downstream consumers (notifications, receipts, UI) act on these.
_OUTBOX_INTENT_STATES = frozenset({
    RecoveryState.PROPOSED, RecoveryState.ESCALATED,
    RecoveryState.VERIFIED_RECOVERY, RecoveryState.RECOVERY_FAILED,
})


class RecoveryEpisodeRepository:
    def __init__(self, db):
        self.db = db

    async def create_episode(self, episode: Dict[str, Any]) -> Dict[str, Any]:
        now = epoch_now()
        episode.setdefault("episode_id", new_episode_id())
        episode.setdefault("created_at", now)
        episode.setdefault("updated_at", now)
        episode.setdefault("state_version", 1)
        episode.setdefault("state", RecoveryState.DETECTED.value)
        episode.setdefault("owner_generation", 0)
        episode.setdefault("attempt_count", 0)
        episode.setdefault("model_calls", 0)
        episode.setdefault("tool_calls", 0)
        episode.setdefault("verification_calls", 0)
        return await self._create_or_get_existing(episode)

    async def _create_or_get_existing(self, episode: Dict[str, Any]) -> Dict[str, Any]:
        """§12.5: atomic creation/dedup — the partial unique index on
        (run_id, task_id) over ACTIVE states makes a concurrent duplicate
        INSERT fail; the loser returns the WINNER's episode so both
        workers claim the same logical recovery."""
        try:
            await self.db.execute(
                """
                INSERT INTO recovery_episodes
                    (episode_id, tenant_id, actor_id, command_id, run_id, task_id,
                     failed_step_id, goal_digest, acceptance_criteria_json,
                     allowed_scope_json, failure_code, failure_category,
                     provider_status, evidence_references_json,
                     proposal_action, proposal_arguments_json, proposal_digest,
                     proposal_explanation, policy_revision, policy_result,
                     policy_reason, owner_worker_id, owner_epoch,
                     owner_generation, lease_expires_at, state, state_version,
                     deadline_at, attempt_count, model_calls, tool_calls,
                     verification_calls, budget_reservation_id,
                     logical_action_id, effect_id, connector_revision_id,
                     credential_binding_id, verification_verdict,
                     verification_evidence_json, incident_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (episode["episode_id"], episode.get("tenant_id"),
                 episode.get("actor_id"), episode.get("command_id"),
                 episode.get("run_id"), episode.get("task_id"),
                 episode.get("failed_step_id"), episode.get("goal_digest"),
                 json.dumps(episode.get("acceptance_criteria") or []),
                 json.dumps(episode.get("allowed_scope") or []),
                 episode.get("failure_code"), episode.get("failure_category"),
                 episode.get("provider_status"),
                 json.dumps(episode.get("evidence_references") or []),
                 episode.get("proposal_action"),
                 json.dumps(episode.get("proposal_arguments") or {}),
                 episode.get("proposal_digest"), episode.get("proposal_explanation"),
                 episode.get("policy_revision"), episode.get("policy_result"),
                 episode.get("policy_reason"), episode.get("owner_worker_id"),
                 episode.get("owner_epoch"), episode.get("owner_generation", 0),
                 episode.get("lease_expires_at"), episode["state"],
                 episode["state_version"], episode.get("deadline_at"),
                 episode.get("attempt_count", 0), episode.get("model_calls", 0),
                 episode.get("tool_calls", 0), episode.get("verification_calls", 0),
                 episode.get("budget_reservation_id"),
                 episode.get("logical_action_id"), episode.get("effect_id"),
                 episode.get("connector_revision_id"),
                 episode.get("credential_binding_id"),
                 episode.get("verification_verdict"),
                 json.dumps(episode.get("verification_evidence") or []),
                 episode.get("incident_id"), episode["created_at"],
                 episode["updated_at"]))
            await self.db.commit()
            return episode
        except Exception:
            await self.db.commit()
            existing = await self.get_episode_for_task(
                episode.get("run_id"), episode.get("task_id"))
            if existing is not None:
                return existing
            raise

    async def get_episode(self, episode_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM recovery_episodes WHERE episode_id=?", (episode_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_episode_for_task(self, run_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM recovery_episodes WHERE run_id=? AND task_id=? "
            "AND state NOT IN ('VERIFIED_RECOVERY','RECOVERY_FAILED',"
            "'ESCALATED','CANCELLED','SUPERSEDED') "
            "ORDER BY created_at DESC LIMIT 1", (run_id, task_id))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def claim_episode(self, episode_id: str, worker_id: str,
                            lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        """§11.7: atomic ownership claim — CAS on the lease + generation."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT owner_generation, tenant_id, state FROM recovery_episodes "
                "WHERE episode_id=?", (episode_id,))
            row = await cursor.fetchone()
            if not row or row["state"] in (
                    RecoveryState.VERIFIED_RECOVERY.value,
                    RecoveryState.RECOVERY_FAILED.value,
                    RecoveryState.ESCALATED.value,
                    RecoveryState.CANCELLED.value,
                    RecoveryState.SUPERSEDED.value):
                return None
            new_gen = int(row["owner_generation"] or 0) + 1
            cursor = await conn.execute(
                "UPDATE recovery_episodes SET owner_worker_id=?, "
                "owner_generation=?, lease_expires_at=?, updated_at=? "
                "WHERE episode_id=? AND (lease_expires_at IS NULL "
                "OR lease_expires_at<=? OR owner_worker_id=?)",
                (worker_id, new_gen, now + lease_seconds, now,
                 episode_id, now, worker_id))
            if not cursor.rowcount:
                return None
        return await self.get_episode(episode_id)

    async def transition(self, episode_id: str, target: RecoveryState, *,
                         actor: str = "system",
                         expected_state: Optional[RecoveryState] = None,
                         expected_version: Optional[int] = None,
                         evidence_reference: Optional[str] = None,
                         error_code: Optional[str] = None,
                         expected_owner_generation: Optional[int] = None,
                         require_live_lease: bool = False) -> Dict[str, Any]:
        """§11.3: atomic CAS transition — expected state + version in the
        UPDATE. Concurrent transitions produce one winner and one conflict.

        §12.5 (review finding G): transitions INTO or WITHIN the protected
        execution states (EXECUTING, VERIFYING) and the verification
        verdicts require the CURRENT ownership: the caller's generation
        must match the durable owner generation (when provided) and the
        lease must be LIVE (when required) — a stale owner whose lease
        expired cannot commit recovery outcomes."""
        now = epoch_now()
        _PROTECTED = {RecoveryState.EXECUTING, RecoveryState.VERIFYING,
                      RecoveryState.VERIFIED_RECOVERY,
                      RecoveryState.RECOVERY_FAILED}
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT * FROM recovery_episodes WHERE episode_id=?",
                (episode_id,))
            row = await cursor.fetchone()
            if not row:
                raise RecoveryTransitionError("Episode not found",
                                              RecoveryState.DETECTED, 0)
            current = RecoveryState(row["state"])
            version = int(row["state_version"])
            if not recovery_transition_valid(current, target, actor):
                raise RecoveryTransitionError(
                    f"Invalid recovery transition {current.value} → "
                    f"{target.value} by {actor}", current, version)
            if expected_state is not None and current is not expected_state:
                raise RecoveryTransitionError(
                    f"Expected state {expected_state.value}, current {current.value}",
                    current, version)
            if expected_version is not None and version != int(expected_version):
                raise RecoveryTransitionError(
                    f"Version conflict for {episode_id}: durable {version}, "
                    f"caller {expected_version}", current, version)
            if (require_live_lease or target in _PROTECTED
                    or current in _PROTECTED):
                lease_expires_at = row["lease_expires_at"]
                owner = row["owner_worker_id"] or ""
                if not owner:
                    raise RecoveryTransitionError(
                        f"Episode {episode_id} has no recovery owner — claim "
                        "it first (§12.5)", current, version)
                if lease_expires_at is None or int(lease_expires_at) <= now:
                    raise RecoveryTransitionError(
                        f"Recovery lease for {episode_id} expired — a stale "
                        "owner cannot commit (§12.5 controlled reacquisition "
                        "required)", current, version)
                if expected_owner_generation is not None and \
                        int(row["owner_generation"] or 0) != int(expected_owner_generation):
                    raise RecoveryTransitionError(
                        f"Ownership generation conflict for {episode_id}: "
                        f"durable {row['owner_generation']}, caller "
                        f"{expected_owner_generation}", current, version)
            new_version = version + 1
            sets = "state=?, state_version=?, updated_at=?"
            args: List[Any] = [target.value, new_version, now]
            if evidence_reference:
                sets += ", evidence_references_json=?"
                row2 = await conn.execute(
                    "SELECT evidence_references_json FROM recovery_episodes WHERE episode_id=?",
                    (episode_id,))
                fetched = await row2.fetchone()
                existing = json.loads(fetched["evidence_references_json"] if fetched else "[]") if fetched else []
                existing.append(evidence_reference)
                args.append(json.dumps(existing))
            if error_code:
                sets += ", error_code=?"
                args.append(error_code)
            args.extend([episode_id, version])
            cursor = await conn.execute(
                f"UPDATE recovery_episodes SET {sets} "
                "WHERE episode_id=? AND state_version=?", args)
            if not cursor.rowcount:
                raise RecoveryTransitionError(
                    f"Concurrent recovery change for {episode_id}", current, version)
            # §12.5: durable transitions AND outbox intents — the states
            # downstream consumers act on commit an outbox event IN THE
            # SAME transaction, so a crash between the transition and any
            # publication loses nothing (the dispatcher re-drives it).
            if target in _OUTBOX_INTENT_STATES:
                import uuid as _uuid
                keys = row.keys()
                await conn.execute(
                    """
                    INSERT INTO outbox_events
                        (event_id, command_id, aggregate_id, aggregate_version,
                         event_type, schema_version, payload_json,
                         created_at, available_at, publish_attempts)
                    VALUES (?, NULL, ?, ?, ?, 1, ?, ?, ?, 0)
                    """,
                    (f"evt_{_uuid.uuid4().hex[:20]}", episode_id, version + 1,
                     f"recovery.{target.value}",
                     json.dumps({
                         "episode_id": episode_id,
                         "tenant_id": row["tenant_id"] if "tenant_id" in keys else None,
                         "run_id": row["run_id"] if "run_id" in keys else None,
                         "task_id": row["task_id"] if "task_id" in keys else None,
                         "from_state": current.value, "to_state": target.value,
                         "actor": actor, "error_code": error_code,
                     }),
                     now, now))
        return await self.get_episode(episode_id)

    async def reviewed_failures(self, *, tenant_id: Optional[str] = None,
                                since_epoch: Optional[int] = None,
                                limit: int = 200) -> List[Dict[str, Any]]:
        """§13.8: the REVIEWED-FAILURE query the improvement proposer
        consumes — TERMINAL failed/escalated episodes only (an active
        abandoned episode is a recovery candidate, not a reviewed
        failure). Tenant-scoped and time-windowed; each row carries the
        failure category, task family context, and evidence references
        the clustering needs."""
        params: List[Any] = []
        sql = ("SELECT * FROM recovery_episodes WHERE state IN "
               "('ESCALATED','RECOVERY_FAILED')")
        if tenant_id:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        if since_epoch is not None:
            sql += " AND updated_at>=?"
            params.append(int(since_epoch))
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(sql, tuple(params))
        return [dict(r) for r in await cursor.fetchall()]

    async def abandoned_episodes(self, lease_grace_seconds: int = 120,
                                 limit: int = 50) -> List[Dict[str, Any]]:
        """§11.8: episodes whose lease expired while still active — the
        reconciler can resume them."""
        cutoff = epoch_now() - lease_grace_seconds
        cursor = await self.db.execute(
            "SELECT * FROM recovery_episodes WHERE state IN "
            "('DETECTED','CLASSIFIED','PROPOSED','VALIDATED','READY',"
            "'EXECUTING','VERIFYING') AND "
            "(lease_expires_at IS NULL OR lease_expires_at<=?) LIMIT ?",
            (cutoff, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def release_ownership(self, episode_id: str, *,
                                owner_worker_id: str,
                                owner_generation: int) -> bool:
        """§29.23: the owner deliberately releases the lease when its
        phase is DONE — after queueing a repair, the recovery worker no
        longer owns the episode; the VERIFIER that checks the repaired
        outcome acquires ownership through claim_episode's controlled
        reacquisition. Owner-bound CAS: a stale worker cannot release a
        newer owner's lease."""
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE recovery_episodes SET lease_expires_at=?, "
                "updated_at=? WHERE episode_id=? AND owner_worker_id=? "
                "AND owner_generation=?",
                (0, now, episode_id, owner_worker_id,
                 int(owner_generation)))
            return bool(cursor.rowcount)

    async def increment_counters(self, episode_id: str, *,
                                 model_calls: int = 0, tool_calls: int = 0,
                                 verification_calls: int = 0,
                                 attempt_count: int = 0) -> None:
        await self.db.execute(
            "UPDATE recovery_episodes SET model_calls=model_calls+?, "
            "tool_calls=tool_calls+?, verification_calls=verification_calls+?, "
            "attempt_count=attempt_count+?, updated_at=? WHERE episode_id=?",
            (model_calls, tool_calls, verification_calls, attempt_count,
             epoch_now(), episode_id))
        await self.db.commit()


_repo: Optional[RecoveryEpisodeRepository] = None


def set_recovery_episode_repo(repo) -> None:
    global _repo
    _repo = repo


def get_recovery_episode_repo() -> Optional[RecoveryEpisodeRepository]:
    return _repo
