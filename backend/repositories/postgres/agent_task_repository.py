"""Durable specialist contracts shared by API and workers on PostgreSQL."""
from __future__ import annotations

import hashlib
import json

from backend.models.admission import epoch_now
from backend.models.agent_task import (
    MAX_ACTIVE_CHILDREN_PER_PARENT, MAX_CHILDREN_PER_PARENT, MAX_DELEGATION_DEPTH,
    AgentTaskContract, AgentTaskError, AgentTaskState, agent_task_transition_valid,
    validate_task_settlement,
)
from backend.repositories.agent_task_repository import AgentTaskRepository, _row_to_contract

ACTIVE = ("PROPOSED", "ASSIGNED", "RUNNING")
DISPATCHED = ("ASSIGNED", "RUNNING")


def _contract(row):
    if row is None:
        return None
    fields = dict(row)
    specification = json.loads(fields.pop("specification"))
    return _row_to_contract({**specification, **fields})


async def _lock_root(conn, root_id):
    # Separate parentless workflows have no row to lock at first admission.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 2604))", root_id)


class PostgresAgentTaskRepository(AgentTaskRepository):
    """Reuse the shared subtree reader and completion-policy evaluation."""

    async def get_contract(self, contract_id):
        return _contract(await self.db.fetch_one("SELECT * FROM agent_tasks WHERE contract_id=$1", contract_id))

    async def children_of(self, parent_id):
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_tasks WHERE parent_id=$1 ORDER BY created_at, contract_id", parent_id)
        return [_contract(row) for row in rows]

    async def find_equivalent_active(self, parent_id, fingerprint):
        for child in await self.children_of(parent_id):
            if not child.terminal:
                key = hashlib.sha256(f"{child.goal}|{child.role.value}".encode()).hexdigest()[:16]
                if key == fingerprint:
                    return child
        return None

    async def create_contract(self, contract: AgentTaskContract):
        if contract.contract_id in [contract.parent_id, *contract.ancestor_ids]:
            raise AgentTaskError("Delegation cycle detected", "DELEGATION_CYCLE")
        if contract.budget_units < 0:
            raise AgentTaskError("Negative contract budget", "BUDGET_EXCEEDED")
        async with self.db.pool.acquire() as conn, conn.transaction():
            root = await conn.fetchval("SELECT root_id FROM agent_tasks WHERE contract_id=$1", contract.parent_id)
            if root is None and contract.parent_kind == "contract":
                raise AgentTaskError("Parent contract not found", "NOT_FOUND")
            root = root or contract.parent_id
            await _lock_root(conn, root)
            parent = _contract(await conn.fetchrow(
                "SELECT * FROM agent_tasks WHERE contract_id=$1 FOR UPDATE", contract.parent_id))
            if parent:
                if parent.tenant_id != contract.tenant_id:
                    raise AgentTaskError("Parent belongs to a different tenant", "PARENT_SCOPE")
                if parent.terminal:
                    raise AgentTaskError("Parent is terminal", "PARENT_CANCELLED" if
                                         parent.state is AgentTaskState.CANCELLED else "PARENT_TERMINAL")
                contract.depth = max(contract.depth, parent.depth + 1)
                contract.ancestor_ids = [*parent.ancestor_ids, parent.contract_id]
            else:
                owner = await conn.fetchval("SELECT user_id FROM workflows WHERE id=$1", contract.parent_id)
                if owner is not None and owner != contract.tenant_id:
                    raise AgentTaskError("Workflow belongs to a different tenant", "PARENT_SCOPE")
            if not 0 <= contract.depth <= MAX_DELEGATION_DEPTH:
                raise AgentTaskError("Delegation depth exceeds the cap", "DEPTH_EXCEEDED")
            siblings = await conn.fetch(
                "SELECT role, tenant_id, goal, state, dedup_key, budget_units, spent_units FROM agent_tasks WHERE parent_id=$1",
                contract.parent_id)
            if any(s["tenant_id"] != contract.tenant_id for s in siblings):
                raise AgentTaskError("Parent belongs to a different tenant", "PARENT_SCOPE")
            active = [s for s in siblings if s["state"] in ACTIVE]
            if contract.dedup_key and any(s["dedup_key"] == contract.dedup_key for s in active):
                raise AgentTaskError("Duplicate active assignment", "DUPLICATE_ASSIGNMENT")
            if any(s["goal"] == contract.goal and s["role"] == contract.role.value for s in active):
                raise AgentTaskError("Equivalent work already delegated", "EQUIVALENT_WORK_ACTIVE")
            if len(siblings) >= MAX_CHILDREN_PER_PARENT:
                raise AgentTaskError("Parent fan-out exceeded", "FAN_OUT_EXCEEDED")
            if sum(s["state"] in DISPATCHED for s in active) >= MAX_ACTIVE_CHILDREN_PER_PARENT:
                raise AgentTaskError("Parent concurrency exceeded", "ACTIVE_CHILDREN_EXCEEDED")
            reserved = sum(s["budget_units"] if s["state"] in ACTIVE else s["spent_units"] for s in siblings)
            if parent and reserved + contract.budget_units > parent.budget_units:
                raise AgentTaskError("Parent budget exceeded", "BUDGET_EXCEEDED")
            now = epoch_now()
            row = await conn.fetchrow("""
                INSERT INTO agent_tasks(contract_id,root_id,parent_id,role,tenant_id,goal,dedup_key,
                    specification,deadline_at,budget_units,created_at,updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$11) RETURNING *
                """, contract.contract_id, root, contract.parent_id, contract.role.value,
                contract.tenant_id, contract.goal, contract.dedup_key, json.dumps(contract.to_row()),
                contract.deadline_at, contract.budget_units, now)
            return _contract(row)

    async def claim(self, contract_id, worker_id, lease_seconds=300):
        if not worker_id or lease_seconds <= 0:
            raise AgentTaskError("Claim requires an owner and a positive lease", "INVALID_CLAIM")
        async with self.db.pool.acquire() as conn, conn.transaction():
            root = await conn.fetchval("SELECT root_id FROM agent_tasks WHERE contract_id=$1", contract_id)
            if root is None:
                return None
            await _lock_root(conn, root)
            row = await conn.fetchrow("SELECT * FROM agent_tasks WHERE contract_id=$1 FOR UPDATE", contract_id)
            now = epoch_now()
            if row["state"] not in ACTIVE:
                return None
            if row["deadline_at"] and row["deadline_at"] <= now:
                await conn.execute("""UPDATE agent_tasks SET state='TIMED_OUT',updated_at=$2,
                                   state_version=state_version+1 WHERE contract_id=$1""", contract_id, now)
                return None
            if row["lease_expires_at"] > now and (row["state"] == "RUNNING" or row["owner_worker_id"] != worker_id):
                return None
            if row["state"] == "PROPOSED":
                count = await conn.fetchval("""SELECT count(*) FROM agent_tasks WHERE parent_id=$1
                                             AND state IN ('ASSIGNED','RUNNING')""", row["parent_id"])
                if count >= MAX_ACTIVE_CHILDREN_PER_PARENT:
                    return None
            claimed = await conn.fetchrow("""UPDATE agent_tasks SET state='RUNNING', owner_worker_id=$2,
                lease_expires_at=$3,updated_at=$4,state_version=state_version+1 WHERE contract_id=$1 RETURNING *""",
                contract_id, worker_id, now + lease_seconds, now)
            return _contract(claimed)

    async def renew_lease(self, contract_id, worker_id, lease_seconds=300):
        now = epoch_now()
        if lease_seconds <= 0:
            return False
        row = await self.db.fetch_one("""UPDATE agent_tasks SET lease_expires_at=$3,updated_at=$4
            WHERE contract_id=$1 AND owner_worker_id=$2 AND state='RUNNING' AND lease_expires_at>$4
            AND (deadline_at=0 OR deadline_at>$4) RETURNING contract_id""", contract_id, worker_id,
            now + lease_seconds, now)
        return row is not None

    async def transition(self, contract_id, target, *, actor="system", expected_version=None,
                         result=None, error=None, spent_units=0):
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM agent_tasks WHERE contract_id=$1 FOR UPDATE", contract_id)
            current = _contract(row)
            if current is None:
                raise AgentTaskError("Contract not found", "NOT_FOUND")
            if not agent_task_transition_valid(current.state, target):
                raise AgentTaskError("Invalid transition", "INVALID_TRANSITION")
            if expected_version is not None and current.state_version != expected_version:
                raise AgentTaskError("Version conflict", "VERSION_CONFLICT")
            now = epoch_now()
            if target is AgentTaskState.COMPLETED and current.deadline_at and current.deadline_at <= now:
                raise AgentTaskError("Deadline passed", "DEADLINE_PASSED")
            if target in (AgentTaskState.COMPLETED, AgentTaskState.FAILED, AgentTaskState.TIMED_OUT):
                if current.owner_worker_id and (current.owner_worker_id != actor or current.lease_expires_at <= now):
                    raise AgentTaskError("Current live claim required", "CLAIM_FENCED")
            validate_task_settlement(target, result, current.output_schema, current.spent_units,
                                     spent_units, current.budget_units)
            updated = await conn.fetchrow("""UPDATE agent_tasks SET state=$2,state_version=state_version+1,
                updated_at=$3,result_json=COALESCE($4,result_json),error=COALESCE($5,error),
                spent_units=spent_units+$6 WHERE contract_id=$1 RETURNING *""",
                contract_id, target.value, now, json.dumps(result) if result is not None else None,
                error, spent_units)
            return _contract(updated)

    async def pending_contracts(self, *, limit=50):
        rows = await self.db.fetch_all("""SELECT * FROM agent_tasks WHERE state='PROPOSED'
            ORDER BY created_at,contract_id LIMIT $1""", limit)
        return [_contract(row) for row in rows]

    async def reclaimable_contracts(self, *, limit=50):
        rows = await self.db.fetch_all("""SELECT * FROM agent_tasks WHERE state='RUNNING'
            AND lease_expires_at>0 AND lease_expires_at<=$1 ORDER BY lease_expires_at,contract_id LIMIT $2""",
            epoch_now(), limit)
        return [_contract(row) for row in rows]

    async def expire_stale_contracts(self, *, now=None):
        result = await self.db.execute("""UPDATE agent_tasks SET state='TIMED_OUT',state_version=state_version+1,
            updated_at=$1 WHERE deadline_at>0 AND deadline_at<=$1 AND state IN ('PROPOSED','ASSIGNED','RUNNING')""",
            epoch_now() if now is None else now)
        return int(result.split()[-1])

    async def cancel_tree(self, contract_id, *, actor="parent"):
        async with self.db.pool.acquire() as conn, conn.transaction():
            root = await conn.fetchval("SELECT root_id FROM agent_tasks WHERE contract_id=$1", contract_id)
            if root is None:
                raise AgentTaskError("Contract not found", "NOT_FOUND")
            await _lock_root(conn, root)
            row = await conn.fetchrow("SELECT state FROM agent_tasks WHERE contract_id=$1 FOR UPDATE", contract_id)
            if row["state"] in ACTIVE:
                await conn.execute("""UPDATE agent_tasks SET state='CANCELLED',state_version=state_version+1,
                                   updated_at=$2,error=$3 WHERE contract_id=$1""",
                                   contract_id, epoch_now(), f"cancelled by {actor}")
            elif row["state"] != "CANCELLED":
                raise AgentTaskError("Terminal contract cannot be cancelled", "INVALID_TRANSITION")
            count = await self._cancel_descendants(conn, contract_id, actor)
            return {"contract_id": contract_id, "state": "CANCELLED", "children_cancelled": count}

    async def cancel_subtree(self, contract_id, *, actor="parent"):
        async with self.db.pool.acquire() as conn, conn.transaction():
            root = await conn.fetchval("SELECT root_id FROM agent_tasks WHERE contract_id=$1", contract_id)
            await _lock_root(conn, root or contract_id)
            return await self._cancel_descendants(conn, contract_id, actor)

    @staticmethod
    async def _cancel_descendants(conn, contract_id, actor):
        result = await conn.execute("""WITH RECURSIVE descendants(id) AS (
            SELECT contract_id FROM agent_tasks WHERE parent_id=$1 UNION
            SELECT t.contract_id FROM agent_tasks t JOIN descendants d ON d.id=t.parent_id
        ) UPDATE agent_tasks SET state='CANCELLED',state_version=state_version+1,updated_at=$2,error=$3
          WHERE contract_id IN (SELECT id FROM descendants) AND state IN ('PROPOSED','ASSIGNED','RUNNING')""",
            contract_id, epoch_now(), f"parent {contract_id} cancelled by {actor}")
        return int(result.split()[-1])
