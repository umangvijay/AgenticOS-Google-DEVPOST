"""Firestore agent-task contract repository (Phase 15 — parity).

The SAME contract as the SQLite AgentTaskRepository: §15.5 bounds and
duplicate-assignment dedup enforced inside async transactions
(deterministic ids, reads before writes), CAS transitions with typed
output validation, atomic ownership claims with live leases.
"""
from __future__ import annotations

import json
import logging
import time as _time
import uuid
from typing import Any, Dict, List, Optional

from google.cloud import firestore as fs_types

from backend.models.admission import epoch_now
from backend.models.agent_task import (
    MAX_ACTIVE_CHILDREN_PER_PARENT,
    MAX_CHILDREN_PER_PARENT,
    MAX_DELEGATION_DEPTH,
    TERMINAL_AGENT_TASK_STATES,
    AgentRole,
    AgentTaskContract,
    AgentTaskError,
    AgentTaskState,
    agent_task_transition_valid,
    validate_task_settlement,
)

logger = logging.getLogger(__name__)

COLLECTION = "agent_tasks"
_TERMINAL_STATES = {s.value for s in TERMINAL_AGENT_TASK_STATES}


def new_contract_id() -> str:
    return f"atc_{uuid.uuid4().hex[:20]}"


def _doc_to_contract(data: Dict[str, Any], contract_id: str) -> AgentTaskContract:
    return AgentTaskContract(
        contract_id=contract_id,
        parent_kind=data.get("parent_kind") or "run",
        parent_id=data.get("parent_id") or "",
        ancestor_ids=list(data.get("ancestor_ids") or []),
        role=AgentRole(data.get("role") or "outcome_verifier"),
        tenant_id=data.get("tenant_id") or "",
        dedup_key=data.get("dedup_key"),
        input_data=dict(data.get("input_data") or {}),
        output_schema=dict(data.get("output_schema") or {}),
        allowed_tools=list(data.get("allowed_tools") or []),
        budget_units=int(data.get("budget_units") or 0),
        deadline_at=int(data.get("deadline_at") or 0),
        depth=int(data.get("depth") or 0),
        completion_policy=data.get("completion_policy") or "require_all",
        goal=data.get("goal") or "",
        exclusions=list(data.get("exclusions") or []),
        input_refs=list(data.get("input_refs") or []),
        required_capabilities=list(data.get("required_capabilities") or []),
        acceptance_criteria=list(data.get("acceptance_criteria") or []),
        retry_policy=data.get("retry_policy") or "bounded",
        agent_version=data.get("agent_version") or "",
        tool_version=data.get("tool_version") or "",
        policy_version=data.get("policy_version") or "",
        state=AgentTaskState(data.get("state") or "PROPOSED"),
        state_version=int(data.get("state_version") or 1),
        owner_worker_id=data.get("owner_worker_id"),
        lease_expires_at=int(data.get("lease_expires_at") or 0),
        result=data.get("result"),
        error=data.get("error"),
        spent_units=int(data.get("spent_units") or 0),
        created_at=int(data.get("created_at") or 0),
        updated_at=int(data.get("updated_at") or 0),
    )


def _contract_to_doc(c: AgentTaskContract) -> Dict[str, Any]:
    return {
        "parent_kind": c.parent_kind, "parent_id": c.parent_id,
        "ancestor_ids": list(c.ancestor_ids), "role": c.role.value,
        "tenant_id": c.tenant_id, "dedup_key": c.dedup_key,
        "input_data": dict(c.input_data),
        "output_schema": dict(c.output_schema),
        "allowed_tools": list(c.allowed_tools),
        "budget_units": int(c.budget_units), "deadline_at": int(c.deadline_at),
        "depth": int(c.depth), "completion_policy": c.completion_policy,
        "goal": c.goal, "exclusions": list(c.exclusions), "input_refs": list(c.input_refs),
        "required_capabilities": list(c.required_capabilities),
        "acceptance_criteria": list(c.acceptance_criteria), "retry_policy": c.retry_policy,
        "agent_version": c.agent_version, "tool_version": c.tool_version, "policy_version": c.policy_version,
        "state": c.state.value, "state_version": int(c.state_version),
        "owner_worker_id": c.owner_worker_id,
        "lease_expires_at": int(c.lease_expires_at or 0),
        "result": c.result, "error": c.error,
        "spent_units": int(c.spent_units),
        "created_at": int(c.created_at or 0),
        "updated_at": int(c.updated_at or 0),
    }


class FirestoreAgentTaskRepository:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def create_contract(self, contract: AgentTaskContract) -> AgentTaskContract:
        db = await self._get_db()
        now = epoch_now()
        contract.created_at = now
        contract.updated_at = now
        ref = db.collection(COLLECTION).document(contract.contract_id)

        @fs_types.async_transactional
        async def create(transaction):
            # §15.5 cycle + depth bounds (identical rules to SQLite).
            if contract.contract_id in (contract.ancestor_ids or []) or \
                    contract.parent_id == contract.contract_id:
                raise AgentTaskError("Delegation cycle detected",
                                     "DELEGATION_CYCLE")
            if contract.depth > MAX_DELEGATION_DEPTH:
                raise AgentTaskError(
                    f"Delegation depth {contract.depth} exceeds the cap "
                    f"{MAX_DELEGATION_DEPTH}", "DEPTH_EXCEEDED")
            # Duplicate assignment dedup (deterministic dedup doc).
            if contract.dedup_key:
                dup_ref = db.collection("agent_task_dedup").document(
                    f"{contract.parent_id}__{contract.dedup_key}")
                dup = await dup_ref.get(transaction=transaction)
                if dup.exists:
                    d = dup.to_dict() or {}
                    if d.get("state") not in _TERMINAL_STATES:
                        raise AgentTaskError(
                            f"Duplicate assignment: "
                            f"{d.get('contract_id')} already covers this work",
                            "DUPLICATE_ASSIGNMENT")
            parents = db.collection(COLLECTION)
            total = 0
            async for _snap in (parents.where("parent_id", "==",
                                              contract.parent_id)
                                .limit(MAX_CHILDREN_PER_PARENT + 1).stream()):
                total += 1
            if total >= MAX_CHILDREN_PER_PARENT:
                raise AgentTaskError(
                    f"Parent {contract.parent_id} already has {total} "
                    f"children (cap {MAX_CHILDREN_PER_PARENT})",
                    "FAN_OUT_EXCEEDED")
            active = 0
            for state in ("PROPOSED", "ASSIGNED", "RUNNING"):
                q = (parents.where("parent_id", "==", contract.parent_id)
                     .where("state", "==", state)
                     .limit(MAX_ACTIVE_CHILDREN_PER_PARENT + 1))
                n = 0
                async for _s in q.stream():
                    n += 1
                active += n
            if active >= MAX_ACTIVE_CHILDREN_PER_PARENT:
                raise AgentTaskError(
                    f"Parent {contract.parent_id} has {active} active "
                    f"children (cap {MAX_ACTIVE_CHILDREN_PER_PARENT})",
                    "ACTIVE_CHILDREN_EXCEEDED")
            transaction.create(ref, _contract_to_doc(contract))
            if contract.dedup_key:
                transaction.set(db.collection("agent_task_dedup").document(
                    f"{contract.parent_id}__{contract.dedup_key}"), {
                        "contract_id": contract.contract_id,
                        "state": contract.state.value,
                    })

        await create(db.transaction())
        return await self.get_contract(contract.contract_id)

    async def get_contract(self, contract_id: str) -> Optional[AgentTaskContract]:
        snap = await (await self._get_db()).collection(COLLECTION).document(
            contract_id).get()
        if not snap.exists:
            return None
        return _doc_to_contract(snap.to_dict() or {}, contract_id)

    async def children_of(self, parent_id: str) -> List[AgentTaskContract]:
        query = ((await self._get_db()).collection(COLLECTION)
                 .where("parent_id", "==", parent_id).limit(100))
        rows = []
        async for snap in query.stream():
            rows.append(_doc_to_contract(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda c: c.created_at)
        return rows

    @staticmethod
    def _expire_if_due(db, transaction, ref, data, cut):
        deadline = int(data.get("deadline_at") or 0)
        if data.get("state") not in ("PROPOSED", "ASSIGNED", "RUNNING") or not 0 < deadline <= cut:
            return False
        transaction.update(ref, {"state": "TIMED_OUT",
                                 "state_version": int(data.get("state_version") or 1) + 1,
                                 "updated_at": cut})
        if data.get("dedup_key"):
            dedup = db.collection("agent_task_dedup").document(
                f"{data['parent_id']}__{data['dedup_key']}")
            transaction.set(dedup, {"contract_id": ref.id, "state": "TIMED_OUT"})
        return True

    async def expire_stale_contracts(self, *, now: Optional[int] = None) -> int:
        db = await self._get_db()
        cut = epoch_now() if now is None else now
        query = (db.collection(COLLECTION).where("state", "in", ["PROPOSED", "ASSIGNED", "RUNNING"])
                 .where("deadline_at", ">", 0).where("deadline_at", "<=", cut).limit(500))

        @fs_types.async_transactional
        async def expire(transaction, ref):
            snapshot = await ref.get(transaction=transaction)
            return self._expire_if_due(db, transaction, ref, snapshot.to_dict() or {}, cut)

        count = 0
        async for snap in query.stream():
            if await expire(db.transaction(), snap.reference):
                count += 1
        return count

    async def reclaimable_contracts(self, *, limit: int = 50) -> List[AgentTaskContract]:
        db = await self._get_db()
        # ASSIGNED rows can survive upgrades from the earlier two-step claim.
        query = (db.collection(COLLECTION).where("state", "in", ["ASSIGNED", "RUNNING"])
                 .where("lease_expires_at", ">", 0).where("lease_expires_at", "<=", epoch_now())
                 .order_by("lease_expires_at").limit(limit))
        return [_doc_to_contract(snap.to_dict() or {}, snap.id) async for snap in query.stream()]

    async def pending_contracts(self, *, limit: int = 50) -> List[AgentTaskContract]:
        """§15.8/§15.28 parity: PROPOSED contracts not yet dispatched —
        the durable dispatch intent the reconciler drains."""
        db = await self._get_db()
        query = (db.collection(COLLECTION).where("state", "==", "PROPOSED")
                 .limit(limit))
        rows = []
        async for snap in query.stream():
            rows.append(_doc_to_contract(snap.to_dict() or {}, snap.id))
        rows.sort(key=lambda c: c.created_at)
        return rows

    async def subtree_of(self, root_id: str) -> List[AgentTaskContract]:
        """§15.2/§15.28 parity: the persisted subtree (BFS by parent
        link) — the hierarchy reconstructs identically on both stores."""
        seen: List[AgentTaskContract] = []
        frontier = [root_id]
        visited = set()
        while frontier:
            pid = frontier.pop(0)
            if pid in visited:
                continue
            visited.add(pid)
            kids = await self.children_of(pid)
            seen.extend(kids)
            frontier.extend(k.contract_id for k in kids)
        return seen

    async def find_equivalent_active(self, parent_id: str, fingerprint: str) \
            -> Optional[AgentTaskContract]:
        """§15.6/§15.28 parity: repeated delegation of equivalent work
        under one parent — fingerprint = goal-digest + role."""
        import hashlib as _hl
        kids = await self.children_of(parent_id)
        for c in kids:
            if c.state.value not in ("PROPOSED", "ASSIGNED", "RUNNING"):
                continue
            fp = _hl.sha256((c.goal + "|" + c.role.value).encode()
                            ).hexdigest()[:16]
            if fp == fingerprint:
                return c
        return None

    async def claim(self, contract_id: str, worker_id: str,
                    lease_seconds: int = 300) -> Optional[AgentTaskContract]:
        if not worker_id or lease_seconds <= 0:
            raise AgentTaskError("Claim requires an owner and a positive lease", "INVALID_CLAIM")
        db = await self._get_db()
        ref = db.collection(COLLECTION).document(contract_id)

        @fs_types.async_transactional
        async def claim_tx(transaction):
            snap = await ref.get(transaction=transaction)
            data = snap.to_dict() or {}
            now = epoch_now()
            state = data.get("state")
            if state not in ("PROPOSED", "ASSIGNED", "RUNNING"):
                return None
            if self._expire_if_due(db, transaction, ref, data, now):
                return None
            if int(data.get("lease_expires_at") or 0) > now:
                if state == "RUNNING" or data.get("owner_worker_id") != worker_id:
                    return None
            update = {"state": "RUNNING", "owner_worker_id": worker_id,
                      "lease_expires_at": now + lease_seconds,
                      "state_version": int(data.get("state_version") or 1) + 1,
                      "updated_at": now}
            transaction.update(ref, update)
            return _doc_to_contract({**data, **update}, contract_id)

        return await claim_tx(db.transaction())

    async def renew_lease(self, contract_id: str, worker_id: str, lease_seconds: int = 300) -> bool:
        if not worker_id or lease_seconds <= 0:
            return False
        db = await self._get_db()
        ref = db.collection(COLLECTION).document(contract_id)

        @fs_types.async_transactional
        async def renew(transaction):
            snap = await ref.get(transaction=transaction)
            data = snap.to_dict() or {}
            now = epoch_now()
            deadline = int(data.get("deadline_at") or 0)
            if (data.get("state") != "RUNNING" or data.get("owner_worker_id") != worker_id
                    or int(data.get("lease_expires_at") or 0) <= now or 0 < deadline <= now):
                return False
            transaction.update(ref, {"lease_expires_at": now + lease_seconds, "updated_at": now})
            return True

        return await renew(db.transaction())

    async def transition(self, contract_id: str, target: AgentTaskState, *,
                         actor: str = "system",
                         expected_version: Optional[int] = None,
                         result: Optional[Dict[str, Any]] = None,
                         error: Optional[str] = None,
                         spent_units: int = 0) -> AgentTaskContract:
        db = await self._get_db()
        now = epoch_now()
        ref = db.collection(COLLECTION).document(contract_id)

        @fs_types.async_transactional
        async def apply(transaction):
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                raise AgentTaskError("Contract not found", "NOT_FOUND")
            data = snap.to_dict() or {}
            current = AgentTaskState(data.get("state") or "PROPOSED")
            version = int(data.get("state_version") or 1)
            if not agent_task_transition_valid(current, target):
                raise AgentTaskError(
                    f"Invalid transition {current.value} → {target.value}",
                    "INVALID_TRANSITION")
            if expected_version is not None and version != int(expected_version):
                raise AgentTaskError(
                    f"Version conflict: durable {version}, caller "
                    f"{expected_version}", "VERSION_CONFLICT")
            if data.get("deadline_at") and int(data["deadline_at"]) <= now \
                    and target is AgentTaskState.COMPLETED:
                raise AgentTaskError(
                    "Deadline passed — completion refused", "DEADLINE_PASSED")
            # §15.9/§15.28: terminal transitions are FENCED on the
            # current claim — the same contract as the SQLite reference.
            if target in (AgentTaskState.COMPLETED, AgentTaskState.FAILED,
                          AgentTaskState.TIMED_OUT):
                owner = data.get("owner_worker_id")
                lease = int(data.get("lease_expires_at") or 0)
                if owner and owner != actor:
                    raise AgentTaskError(
                        f"Stale worker: {actor} does not hold the claim "
                        f"({owner} does)", "CLAIM_FENCED")
                if owner and lease <= now:
                    raise AgentTaskError(
                        "Stale worker: the lease expired — reclaim or "
                        "take over first", "CLAIM_FENCED")
            update: Dict[str, Any] = {
                "state": target.value, "state_version": version + 1,
                "updated_at": now,
            }
            validate_task_settlement(target, result, data.get("output_schema") or {},
                                     int(data.get("spent_units") or 0), spent_units,
                                     int(data.get("budget_units") or 0))
            if result is not None:
                update["result"] = result
            if error is not None:
                update["error"] = error
            if spent_units:
                update["spent_units"] = int(data.get("spent_units") or 0) + int(spent_units)
            transaction.update(ref, update)
            if data.get("dedup_key") and target in TERMINAL_AGENT_TASK_STATES:
                transaction.set(db.collection("agent_task_dedup").document(
                    f"{data.get('parent_id')}__{data['dedup_key']}"), {
                        "contract_id": contract_id,
                        "state": target.value,
                    }, merge=True)

        await apply(db.transaction())
        return await self.get_contract(contract_id)

    async def evaluate_completion_policy(self, parent_contract_id: str
                                         ) -> Dict[str, Any]:
        children = await self.children_of(parent_contract_id)
        completed = [c for c in children if c.state is AgentTaskState.COMPLETED]
        pending = [c for c in children
                   if c.state not in TERMINAL_AGENT_TASK_STATES]
        failed = [c for c in children
                  if c.state in (AgentTaskState.FAILED,
                                 AgentTaskState.TIMED_OUT,
                                 AgentTaskState.CANCELLED)]
        contract = await self.get_contract(parent_contract_id)
        policy = contract.completion_policy if contract else "require_all"
        if pending:
            verdict = "waiting"
        elif policy == "require_all":
            verdict = "complete" if not failed and completed else "incomplete"
        elif policy == "require_any":
            verdict = "complete" if completed else "incomplete"
        else:
            verdict = "complete" if completed else "incomplete"
        return {"verdict": verdict, "policy": policy,
                "completed": [c.contract_id for c in completed],
                "failed": [{"contract_id": c.contract_id,
                            "state": c.state.value, "error": c.error}
                           for c in failed],
                "pending": [c.contract_id for c in pending],
                # §28.5: a partial success (best_effort/require_any with a
                # completed child) is never silent — the verdict names the
                # children that did NOT make it, on every policy.
                "missing": [c.contract_id for c in failed]}

    async def cancel_subtree(self, contract_id: str, *,
                             actor: str = "parent") -> int:
        cancelled = 0
        for child in await self.children_of(contract_id):
            if child.terminal:
                continue
            try:
                await self.transition(child.contract_id,
                                      AgentTaskState.CANCELLED, actor=actor,
                                      error=f"parent {contract_id} cancelled")
                cancelled += 1
            except AgentTaskError:
                pass
        return cancelled
