"""Agent task contract repository (Phase 15).

CAS state transitions, atomic ownership claims, dedup on
(parent, dedup_key), and the §15.5 bounds (depth, fan-out, concurrent
children, cumulative spend) enforced INSIDE the creation/claim
transactions — not in the caller. SQLite reference; Firestore parity
follows the shared state-machine tables.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.models.agent_task import (
    ALLOWED_AGENT_TASK_TRANSITIONS,
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


def new_contract_id() -> str:
    return f"atc_{uuid.uuid4().hex[:20]}"


def _row_to_contract(row) -> AgentTaskContract:
    d = dict(row)
    import json as _json
    return AgentTaskContract(
        contract_id=d["contract_id"],
        parent_kind=d.get("parent_kind") or "run",
        parent_id=d.get("parent_id") or "",
        ancestor_ids=_json.loads(d.get("ancestor_ids_json") or "[]"),
        role=AgentRole(d.get("role") or "outcome_verifier"),
        tenant_id=d.get("tenant_id") or "",
        dedup_key=d.get("dedup_key"),
        input_data=_json.loads(d.get("input_data_json") or "{}"),
        output_schema=_json.loads(d.get("output_schema_json") or "{}"),
        allowed_tools=_json.loads(d.get("allowed_tools_json") or "[]"),
        budget_units=int(d.get("budget_units") or 0),
        deadline_at=int(d.get("deadline_at") or 0),
        depth=int(d.get("depth") or 0),
        completion_policy=d.get("completion_policy") or "require_all",
        goal=d.get("goal") or "",
        exclusions=_json.loads(d.get("exclusions_json") or "[]"),
        input_refs=_json.loads(d.get("input_refs_json") or "[]"),
        required_capabilities=_json.loads(
            d.get("required_capabilities_json") or "[]"),
        acceptance_criteria=_json.loads(
            d.get("acceptance_criteria_json") or "[]"),
        retry_policy=d.get("retry_policy") or "bounded",
        agent_version=d.get("agent_version") or "",
        tool_version=d.get("tool_version") or "",
        policy_version=d.get("policy_version") or "",
        state=AgentTaskState(d.get("state") or "PROPOSED"),
        state_version=int(d.get("state_version") or 1),
        owner_worker_id=d.get("owner_worker_id"),
        lease_expires_at=int(d.get("lease_expires_at") or 0),
        result=(_json.loads(d["result_json"]) if d.get("result_json") else None),
        error=d.get("error"),
        spent_units=int(d.get("spent_units") or 0),
        created_at=int(d.get("created_at") or 0),
        updated_at=int(d.get("updated_at") or 0),
    )


class AgentTaskRepository:
    def __init__(self, db):
        self.db = db

    # ── creation with §15.5 bounds ─────────────────────────────────────

    async def create_contract(self, contract: AgentTaskContract) -> AgentTaskContract:
        now = epoch_now()
        contract.created_at = now
        contract.updated_at = now
        async with self.db.write_transaction() as conn:
            # Cycle detection (§15.5): a contract's ancestor chain must not
            # contain itself; the depth cap subsumes runaway chains.
            if contract.contract_id in (contract.ancestor_ids or []) or \
                    contract.parent_id == contract.contract_id:
                raise AgentTaskError("Delegation cycle detected",
                                     "DELEGATION_CYCLE")
            if contract.depth > MAX_DELEGATION_DEPTH:
                raise AgentTaskError(
                    f"Delegation depth {contract.depth} exceeds the cap "
                    f"{MAX_DELEGATION_DEPTH}", "DEPTH_EXCEEDED")
            # §15.16: cancellation stops NEW child work — a cancelled
            # parent cannot admit further delegation.
            cursor = await conn.execute(
                "SELECT state FROM agent_tasks WHERE contract_id=?",
                (contract.parent_id,))
            parent_row = await cursor.fetchone()
            if parent_row and parent_row["state"] == "CANCELLED":
                raise AgentTaskError(
                    f"Parent {contract.parent_id} is CANCELLED — no new "
                    "child work is admitted (§15.16)",
                    "PARENT_CANCELLED")
            dup = None
            if contract.dedup_key:
                cursor = await conn.execute(
                    "SELECT contract_id FROM agent_tasks WHERE parent_id=? "
                    "AND dedup_key=? AND state NOT IN "
                    "('COMPLETED','FAILED','TIMED_OUT','CANCELLED','SUPERSEDED')",
                    (contract.parent_id, contract.dedup_key))
                dup = await cursor.fetchone()
            if dup:
                raise AgentTaskError(
                    f"Duplicate assignment: {dup['contract_id']} already "
                    "covers this work", "DUPLICATE_ASSIGNMENT")
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM agent_tasks WHERE parent_id=?",
                (contract.parent_id,))
            total_children = int((await cursor.fetchone())["n"])
            if total_children >= MAX_CHILDREN_PER_PARENT:
                raise AgentTaskError(
                    f"Parent {contract.parent_id} already has "
                    f"{total_children} children (cap "
                    f"{MAX_CHILDREN_PER_PARENT})", "FAN_OUT_EXCEEDED")
            # §15.6 CONCURRENCY = DISPATCHED work (ASSIGNED/RUNNING) —
            # a queued intent (PROPOSED) occupies no specialist; counting
            # it would make the fan-out cap unreachable.
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM agent_tasks WHERE parent_id=? "
                "AND state IN ('ASSIGNED','RUNNING')",
                (contract.parent_id,))
            active_children = int((await cursor.fetchone())["n"])
            if active_children >= MAX_ACTIVE_CHILDREN_PER_PARENT:
                raise AgentTaskError(
                    f"Parent {contract.parent_id} has {active_children} "
                    "active children (cap "
                    f"{MAX_ACTIVE_CHILDREN_PER_PARENT})",
                    "ACTIVE_CHILDREN_EXCEEDED")
            # §15.7: cumulative RESERVATION bound — children RESERVE part
            # of the parent's allowance. The sum of ACTIVE children's
            # reserved budgets (not settled spend) cannot exceed the
            # parent's cap: parallel children share the allowance, they
            # do not each get the full remaining amount.
            cursor = await conn.execute(
                "SELECT COALESCE(SUM(budget_units), 0) AS s FROM agent_tasks "
                "WHERE parent_id=? AND state IN "
                "('PROPOSED','ASSIGNED','RUNNING')",
                (contract.parent_id,))
            reserved = int((await cursor.fetchone())["s"])
            parent_budget_cap = await _max_parent_budget(conn, contract.parent_id)
            if contract.budget_units and parent_budget_cap and \
                    reserved + contract.budget_units > parent_budget_cap:
                raise AgentTaskError(
                    f"Delegation would exceed the parent's budget "
                    f"reservation: {reserved} of {parent_budget_cap} "
                    "already reserved by active children",
                    "BUDGET_EXCEEDED")
            row = contract.to_row()
            cols = ("contract_id", "parent_kind", "parent_id",
                    "ancestor_ids_json", "role", "tenant_id", "dedup_key",
                    "input_data_json", "output_schema_json",
                    "allowed_tools_json", "budget_units", "deadline_at",
                    "depth", "completion_policy",
                    "goal", "exclusions_json", "input_refs_json",
                    "required_capabilities_json", "acceptance_criteria_json",
                    "retry_policy", "agent_version", "tool_version",
                    "policy_version",
                    "state", "state_version",
                    "owner_worker_id", "lease_expires_at", "result_json",
                    "error", "spent_units", "created_at", "updated_at")
            await conn.execute(
                f"INSERT INTO agent_tasks ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                tuple(row[c] for c in cols))
        return await self.get_contract(contract.contract_id)

    async def get_contract(self, contract_id: str) -> Optional[AgentTaskContract]:
        cursor = await self.db.execute(
            "SELECT * FROM agent_tasks WHERE contract_id=?", (contract_id,))
        row = await cursor.fetchone()
        return _row_to_contract(row) if row else None

    async def children_of(self, parent_id: str) -> List[AgentTaskContract]:
        cursor = await self.db.execute(
            "SELECT * FROM agent_tasks WHERE parent_id=? ORDER BY created_at",
            (parent_id,))
        return [_row_to_contract(r) for r in await cursor.fetchall()]

    # ── ownership claim (CAS lease) ───────────────────────────────────

    async def claim(self, contract_id: str, worker_id: str,
                    lease_seconds: int = 300) -> Optional[AgentTaskContract]:
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state, state_version, deadline_at, owner_worker_id, "
                "lease_expires_at FROM agent_tasks WHERE contract_id=?",
                (contract_id,))
            row = await cursor.fetchone()
            if not row or row["state"] not in ("PROPOSED", "ASSIGNED",
                                               "RUNNING"):
                return None
            if row["deadline_at"] and int(row["deadline_at"]) <= now:
                await conn.execute(
                    "UPDATE agent_tasks SET state='TIMED_OUT', "
                    "state_version=state_version+1, updated_at=? "
                    "WHERE contract_id=? AND state IN "
                    "('PROPOSED','ASSIGNED','RUNNING')",
                    (now, contract_id))
                return None
            lease = row["lease_expires_at"]
            if lease and int(lease) > now and \
                    row["owner_worker_id"] != worker_id:
                return None
            # §15.9/§15.17: a RUNNING contract with an EXPIRED lease is
            # take-overable — worker loss is recovered by claim takeover
            # (the new owner reclaims with a fresh lease; the state stays
            # RUNNING, the ownership moves).
            if row["state"] == "RUNNING" and lease and int(lease) > now:
                return None   # live lease on a running contract
            target = AgentTaskState.RUNNING
            await conn.execute(
                "UPDATE agent_tasks SET state=?, owner_worker_id=?, "
                "lease_expires_at=?, state_version=state_version+1, "
                "updated_at=? WHERE contract_id=? AND state_version=?",
                (target.value, worker_id, now + lease_seconds, now,
                 contract_id, row["state_version"]))
        return await self.get_contract(contract_id)

    async def renew_lease(self, contract_id: str, worker_id: str,
                          lease_seconds: int = 300) -> bool:
        now = epoch_now()
        cursor = await self.db.execute(
            "UPDATE agent_tasks SET lease_expires_at=?, updated_at=? "
            "WHERE contract_id=? AND owner_worker_id=? AND "
            "lease_expires_at>? AND state='RUNNING'",
            (now + lease_seconds, now, contract_id, worker_id, now))
        await self.db.commit()
        return bool(cursor.rowcount)

    # ── CAS transitions ────────────────────────────────────────────────

    async def transition(self, contract_id: str, target: AgentTaskState, *,
                         actor: str = "system",
                         expected_version: Optional[int] = None,
                         result: Optional[Dict[str, Any]] = None,
                         error: Optional[str] = None,
                         spent_units: int = 0) -> AgentTaskContract:
        now = epoch_now()
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT state, state_version, deadline_at, output_schema_json, "
                "spent_units, budget_units, owner_worker_id, lease_expires_at "
                "FROM agent_tasks WHERE contract_id=?",
                (contract_id,))
            row = await cursor.fetchone()
            if not row:
                raise AgentTaskError("Contract not found", "NOT_FOUND")
            current = AgentTaskState(row["state"])
            version = int(row["state_version"])
            if not agent_task_transition_valid(current, target):
                raise AgentTaskError(
                    f"Invalid transition {current.value} → {target.value}",
                    "INVALID_TRANSITION")
            if expected_version is not None and version != int(expected_version):
                raise AgentTaskError(
                    f"Version conflict: durable {version}, caller "
                    f"{expected_version}", "VERSION_CONFLICT")
            if row["deadline_at"] and int(row["deadline_at"]) <= now and \
                    target is AgentTaskState.COMPLETED:
                raise AgentTaskError(
                    "Deadline passed — completion refused (TIMED_OUT)",
                    "DEADLINE_PASSED")
            # §15.9: terminal transitions are FENCED — only the CURRENT
            # claim owner (with a live lease) may publish output,
            # complete, or fail. An expired worker cannot overwrite a
            # replacement worker's result, and a completed task cannot
            # be failed after the fact.
            if target in (AgentTaskState.COMPLETED, AgentTaskState.FAILED,
                          AgentTaskState.TIMED_OUT):
                owner = row["owner_worker_id"]
                lease = int(row["lease_expires_at"] or 0)
                if owner and owner != actor:
                    raise AgentTaskError(
                        f"Stale worker: {actor} does not hold the claim "
                        f"({owner} does)", "CLAIM_FENCED")
                if owner and lease <= now:
                    raise AgentTaskError(
                        "Stale worker: the lease expired — reclaim or "
                        "take over first", "CLAIM_FENCED")
            validate_task_settlement(target, result, json.loads(row["output_schema_json"] or "{}"),
                                     row["spent_units"], spent_units, row["budget_units"])
            result_json = json.dumps(result) if result is not None else None
            await conn.execute(
                "UPDATE agent_tasks SET state=?, state_version=?, "
                "updated_at=?, result_json=COALESCE(?, result_json), "
                "error=COALESCE(?, error), spent_units=spent_units+? "
                "WHERE contract_id=? AND state_version=?",
                (target.value, version + 1, now, result_json, error,
                 int(spent_units or 0), contract_id, version))
        return await self.get_contract(contract_id)

    # ── §15.6: partial failure evaluation ──────────────────────────────

    @staticmethod
    def _sibling_conclusion(contract: "AgentTaskContract") -> Optional[str]:
        """The child's CONCLUSION for disagreement detection — the first
        recognized decision field of its typed result, else None."""
        result = contract.result if isinstance(contract.result, dict) else {}
        for key in ("verdict", "answer", "decision", "outcome",
                    "conclusion", "status"):
            value = result.get(key)
            if value is not None:
                return str(value).strip().lower()
        return None

    async def evaluate_completion_policy(self, parent_contract_id: str
                                         ) -> Dict[str, Any]:
        """How a parent proceeds when children fail or time out (§15.6):
        require_all → all children must COMPLETE; require_any → at least
        one; best_effort → proceed with whatever completed. Incomplete
        combined results are never silently accepted — the verdict names
        what is missing. §29.22: siblings asked the SAME question (same
        role + normalized goal) that return DIFFERENT conclusions
        surface a 'disputed' verdict — contradiction is never collapsed
        into a silent success; resolving it is the parent's job, with
        the disagreeing evidence named."""
        children = await self.children_of(parent_contract_id)
        completed = [c for c in children
                     if c.state is AgentTaskState.COMPLETED]
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

        # §29.22: contradictory sibling results — same question, different
        # answers. The disagreement is SURFACED with both sides' evidence.
        contradictions: List[Dict[str, Any]] = []
        by_question: Dict[tuple, List] = {}
        for c in completed:
            key = (c.role.value if hasattr(c.role, "value") else str(c.role),
                   " ".join(str(c.goal or "").split()).lower())
            by_question.setdefault(key, []).append(c)
        for (role, goal), siblings in by_question.items():
            if len(siblings) < 2:
                continue
            conclusions: Dict[str, List[str]] = {}
            for c in siblings:
                conclusion = self._sibling_conclusion(c)
                if conclusion is None:
                    continue
                conclusions.setdefault(conclusion, []).append(c.contract_id)
            if len(conclusions) > 1:
                contradictions.append({
                    "role": role,
                    "goal": goal[:120],
                    "positions": [
                        {"conclusion": conclusion,
                         "contract_ids": contract_ids}
                        for conclusion, contract_ids in conclusions.items()],
                })
        if contradictions and verdict == "complete":
            verdict = "disputed"
        return {
            "verdict": verdict,
            "policy": policy,
            "completed": [c.contract_id for c in completed],
            "failed": [{"contract_id": c.contract_id, "state": c.state.value,
                        "error": c.error} for c in failed],
            "pending": [c.contract_id for c in pending],
            # §28.5: a partial success (best_effort/require_any with a
            # completed child) is never silent — the verdict names the
            # children that did NOT make it, on every policy.
            "missing": [c.contract_id for c in failed],
            # §29.22: contradictory sibling outputs — the parent decides
            # with named evidence; the join never silently picks a side.
            "contradictions": contradictions,
        }

    async def expire_stale_contracts(self, *, now: Optional[int] = None) -> int:
        """§15.3/§15.17 (finding I): contracts whose deadline passed —
        PROPOSED/ASSIGNED (never dispatched) and RUNNING (a lost worker
        that cannot complete) — transition honestly to TIMED_OUT. A
        deadline is a real boundary: without this sweep a stuck RUNNING
        contract blocks its parent forever."""
        cut = int(now or epoch_now())
        async with self.db.write_transaction() as conn:
            cursor = await conn.execute(
                "UPDATE agent_tasks SET state='TIMED_OUT', "
                "state_version=state_version+1, updated_at=? "
                "WHERE deadline_at>0 AND deadline_at<=? AND state IN "
                "('PROPOSED','ASSIGNED','RUNNING')",
                (cut, cut))
            n = cursor.rowcount or 0
        return n

    async def reclaimable_contracts(self, *, limit: int = 50)             -> List[AgentTaskContract]:
        """§15.9/§15.17 (finding I/J): RUNNING contracts whose lease
        EXPIRED — the worker-loss recovery set. Claim takeover (§15.9)
        is the recovery path; this is the reconciler's queue for it."""
        cursor = await self.db.execute(
            "SELECT * FROM agent_tasks WHERE state='RUNNING' AND "
            "lease_expires_at>0 AND lease_expires_at<=? ORDER BY "
            "lease_expires_at LIMIT ?", (epoch_now(), limit))
        return [_row_to_contract(r) for r in await cursor.fetchall()]

    async def pending_contracts(self, *, limit: int = 50) -> List[AgentTaskContract]:
        """§15.8: PROPOSED contracts not yet dispatched — the durable
        dispatch intent. A crash between persistence and publication is
        recovered by draining these (create→publish crash window loses
        nothing); duplicate publication cannot create duplicate logical
        execution (the dedup identity already prevented that at
        creation)."""
        cursor = await self.db.execute(
            "SELECT * FROM agent_tasks WHERE state='PROPOSED' "
            "ORDER BY created_at LIMIT ?", (limit,))
        return [_row_to_contract(r) for r in await cursor.fetchall()]

    async def subtree_of(self, root_id: str) -> List[AgentTaskContract]:
        """§15.2: the FULL delegation subtree under a root (BFS by
        parent linkage) — the hierarchy reconstruction after a restart
        uses the PERSISTED parent/child identities, never in-process
        state."""
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

    async def find_equivalent_active(self, parent_id: str, fingerprint: str)             -> Optional[AgentTaskContract]:
        """§15.6: repeated delegation of EQUIVALENT work — an active
        child of the same parent whose fingerprint matches (the
        goal-digest + role). Cycles where agents repeatedly delegate
        back and forth resolve here or via ancestor checks."""
        cursor = await self.db.execute(
            "SELECT * FROM agent_tasks WHERE parent_id=? AND state IN "
            "('PROPOSED','ASSIGNED','RUNNING')", (parent_id,))
        import json as _json
        import hashlib as _hl
        for row in await cursor.fetchall():
            c = _row_to_contract(row)
            fp = _hl.sha256((c.goal + "|" + c.role.value).encode()
                            ).hexdigest()[:16]
            if fp == fingerprint:
                return c
        return None

    async def cancel_subtree(self, contract_id: str, *,
                             actor: str = "parent") -> int:
        """Cancellation propagation (§15.4): cancelling a parent cancels
        its active children (real effects are never undone)."""
        cancelled = 0
        children = await self.children_of(contract_id)
        for child in children:
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


async def _max_parent_budget(conn, parent_id: str) -> int:
    cursor = await conn.execute(
        "SELECT budget_units FROM agent_tasks WHERE contract_id=?",
        (parent_id,))
    row = await cursor.fetchone()
    return int(row["budget_units"]) if row and row["budget_units"] else 0


_repo: Optional[AgentTaskRepository] = None


def set_agent_task_repo(repo) -> None:
    global _repo
    _repo = repo


def get_agent_task_repo() -> Optional[AgentTaskRepository]:
    return _repo
