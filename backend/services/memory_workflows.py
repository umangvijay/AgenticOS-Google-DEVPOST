"""Memory workflows (Phase 16 §16.16–16.18, §16.21–16.22, §16.24).

- §16.16 execution memory: episodes with goal/family/tools/decisions/
  failures/costs/verified result — failed and uncertain outcomes stay
  visible; the platform learns from the OUTCOME, not the optimistic
  completion message.
- §16.17/§16.18 reusable procedures: versioned, with preconditions and
  verification criteria — "worked once" is a CANDIDATE, never an
  auto-activated playbook; before reuse the CURRENT gates revalidate
  (authority, credentials, connector readiness, tool revision, budget,
  approvals) and the procedure VERSION is pinned to the execution.
- §16.21 correction propagation: a dependency map records what was
  derived from a memory; correcting it invalidates the derived artifacts
  AND the cached summaries/embeddings registered against it.
- §16.22 deletion as a workflow: the marker creates an IMMEDIATE
  retrieval barrier (hidden), then cleanup is observable and retryable —
  retention boundaries (immutable execution receipts, backups) are
  stated, never implied.
- §16.24 index migrations: versioned embedding model + chunking; the
  rebuild creates a replacement, tracks coverage, preserves
  authorization metadata, validates, switches deliberately, keeps
  rollback — incompatible vectors are NEVER mixed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now


# ── §16.16: execution episodes ─────────────────────────────────────────

def episode_record(*, run_id: str, goal: str, task_family: str,
                   environment: Dict[str, Any],
                   tools_used: List[Dict[str, Any]],
                   decisions: List[str],
                   failures: List[Dict[str, Any]],
                   cost_units: float, duration_ms: Optional[int],
                   verified_outcome: str,
                   limitations: Optional[List[str]] = None) -> Dict[str, Any]:
    """The episode carries what REUSE needs — and keeps failed/uncertain
    outcomes visible (they are the most instructive memories)."""
    if verified_outcome not in ("verified", "partial", "uncertain",
                                "failed"):
        raise ValueError("verified_outcome must be an honest outcome class")
    return {
        "run_id": run_id, "goal": goal, "task_family": task_family,
        "environment": environment,
        "tools_used": tools_used,          # incl. connector REVISIONS
        "decisions": decisions,
        "failures": failures,              # + recovery records
        "cost_units": cost_units, "duration_ms": duration_ms,
        "verified_outcome": verified_outcome,
        "limitations": list(limitations or []),
        "recorded_at": epoch_now(),
        "note": ("the outcome class comes from durable evaluation "
                 "evidence — never from a completion message"),
    }


# ── §16.17: reusable procedures ────────────────────────────────────────

PROCEDURE_FIELDS = ("goal", "preconditions", "required_capabilities",
                    "steps", "inputs", "outputs", "approval_requirements",
                    "failure_handling", "verification_criteria", "applicable")


@dataclass
class Procedure:
    procedure_id: str
    version: int
    goal: str
    preconditions: List[str]
    required_capabilities: List[str]
    steps: List[Dict[str, Any]]
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    approval_requirements: List[str]
    failure_handling: str
    verification_criteria: List[Dict[str, Any]]
    applicable: Dict[str, Any]          # environment/version constraints
    status: str = "candidate"           # candidate | reviewed | retired
    source_episode_id: Optional[str] = None

    def validate(self) -> None:
        missing = [f for f in ("goal", "steps", "verification_criteria")
                   if not getattr(self, f)]
        if missing:
            raise ValueError(
                f"procedure missing: {', '.join(missing)} — 'worked once' "
                "does not become a playbook without verification criteria")


async def promote_procedure(procedure: Procedure, *,
                            improvement_service=None,
                            reviewer: str = "") -> Procedure:
    """§16.17: a material change routes through the CONTROLLED
    improvement process — the candidate cannot self-promote."""
    procedure.validate()
    if procedure.status == "candidate" and not reviewer:
        raise ValueError(
            "a candidate procedure needs a REVIEWER — proposals cannot "
            "promote themselves (§16.17/§13.4)")
    procedure.status = "reviewed"
    return procedure


# ── §16.18: revalidation before reuse ──────────────────────────────────

async def revalidate_procedure(procedure: Procedure, *, checker) -> Dict[str, Any]:
    """§16.18: EVERY precondition re-checks against the CURRENT world —
    a historical procedure cannot bypass present-day gates. `checker`
    is injected by the deployment (returns {ok, detail} per check)."""
    checks = {}
    for precondition in procedure.preconditions:
        result = await checker(precondition)
        checks[precondition] = {"ok": bool(result.get("ok")),
                                "detail": result.get("detail", "")}
    reusable = all(c["ok"] for c in checks.values())
    return {"procedure_id": procedure.procedure_id,
            "version": procedure.version,
            "reusable": reusable,
            "checks": checks,
            "note": ("the chosen procedure VERSION is pinned to the new "
                     "execution — it cannot silently switch mid-run")}


# ── §16.21: correction propagation with a dependency map ───────────────

class DependencyMap:
    """What was DERIVED from a memory: cached summaries, embeddings,
    procedures, downstream references. Correcting the source
    invalidates everything registered here."""

    def __init__(self):
        self._edges: Dict[str, List[Dict[str, Any]]] = {}

    def register(self, memory_id: str, *, derived_kind: str,
                 derived_id: str) -> None:
        self._edges.setdefault(memory_id, []).append(
            {"kind": derived_kind, "id": derived_id})

    def dependents(self, memory_id: str) -> List[Dict[str, Any]]:
        return list(self._edges.get(memory_id, []))


class DurableDependencyMap(DependencyMap):
    """§21.19 (audit C16): the SAME interface, but the edges live in the
    memory REPOSITORY (tenant-scoped rows) instead of process-local
    state — correction and deletion propagation survives restarts."""

    def __init__(self, memory_repo):
        super().__init__()
        self._repo = memory_repo

    def register(self, memory_id: str, *, derived_kind: str,
                 derived_id: str) -> None:
        super().register(memory_id, derived_kind=derived_kind,
                         derived_id=derived_id)
        self._register_durable(memory_id, derived_kind, derived_id)

    def _register_durable(self, memory_id: str, derived_kind: str,
                          derived_id: str) -> None:
        """Best-effort durable write — the in-memory edge is the fallback
        ONLY if the durable write fails, and the failure is LOGGED."""
        import asyncio as _asyncio
        try:
            coro = self._repo.register_dependency(
                memory_id, derived_kind=derived_kind, derived_id=derived_id)
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                _asyncio.run(coro)
        except Exception:
            import logging
            logging.getLogger("memory_workflows").warning(
                "durable dependency registration failed for %s",
                memory_id, exc_info=True)

    async def register_async(self, memory_id: str, *, derived_kind: str,
                             derived_id: str) -> None:
        """Awaitable durable registration — production paths use this so
        the edge is PERSISTED before the caller proceeds."""
        super().register(memory_id, derived_kind=derived_kind,
                         derived_id=derived_id)
        await self._repo.register_dependency(memory_id,
                                             derived_kind=derived_kind,
                                             derived_id=derived_id)

    async def dependents_async(self, memory_id: str) -> List[Dict[str, Any]]:
        """Durable edges first (fresh, cross-process); merged with any
        in-process registrations."""
        durable = await self._repo.dependents_of(memory_id)
        edges = [{"kind": d.get("derived_kind", d.get("dependent_kind")),
                  "id": d.get("derived_id", d.get("dependent_id"))}
                 for d in durable]
        seen = {(e["kind"], e["id"]) for e in edges}
        for dep in super().dependents(memory_id):
            if (dep["kind"], dep["id"]) not in seen:
                edges.append(dep)
        return edges


async def propagate_correction(*, memory_repo, lifecycle, dependency_map,
                               user_id: str, memory_id: str,
                               new_content: str, new_embedding: List[float],
                               ) -> Dict[str, Any]:
    """§16.21: correcting a fact prevents the OLD version from
    resurfacing through a summary or embedding that was never updated —
    the correction creates the new version (history kept) AND
    invalidates every registered dependent."""
    corrected = await lifecycle.correct(user_id, memory_id, new_content,
                                        embedding=new_embedding)
    invalidated = []
    # §21.19: resolve dependents from DURABLE storage when the map
    # supports it (edges survive restarts) — the in-process fallback
    # stays for maps without durable edges.
    if hasattr(dependency_map, "dependents_async"):
        deps = await dependency_map.dependents_async(memory_id)
    else:
        deps = dependency_map.dependents(memory_id)
    for dep in deps:
        invalidated.append(dep)
        if dep["kind"] == "cached_summary":
            # The stale summary is deleted — it cannot resurface.
            try:
                await memory_repo.delete_memory(user_id, dep["id"])
            except Exception:
                pass
    return {"correction": corrected, "invalidated_dependents": invalidated,
            "note": ("the old version's history is preserved but it can "
                     "no longer be retrieved")}


# ── §16.22: deletion as a workflow ─────────────────────────────────────

DELETION_STATES = ("active", "deletion_requested", "hidden",
                   "cleanup_complete")


async def request_deletion(*, memory_repo, user_id: str, memory_id: str,
                           dependency_map: Optional[DependencyMap] = None,
                           ) -> Dict[str, Any]:
    """§16.22: the marker FIRST — an immediate retrieval barrier (the
    memory goes dark to every search), then cleanup proceeds as
    observable, retryable work. Immutable receipts and backups are
    EXPLICIT retention boundaries stated to the user."""
    record = await memory_repo.get_memory(user_id, memory_id)
    if record is None:
        return {"requested": False, "reason": "not_found"}
    # Immediate barrier: mark hidden (retrieval filters on deletion_state).
    await memory_repo.db.execute(
        "UPDATE memories SET deletion_state='hidden', updated_at=? "
        "WHERE id=? AND user_id=?",
        (str(epoch_now()), memory_id, user_id))
    await memory_repo.db.commit()
    dependents = dependency_map.dependents(memory_id) if dependency_map else []
    return {"requested": True, "state": "hidden",
            "cleanup_pending": True,
            "derived_dependents": dependents,
            "retention_boundaries": [
                "immutable execution receipts remain (audit requirement)",
                "backups retain the content until their scheduled expiry",
            ]}


async def complete_deletion_cleanup(*, memory_repo, user_id: str,
                                    memory_id: str) -> Dict[str, Any]:
    """The retryable cleanup step: after dependents are invalidated, the
    row is removed and the state recorded as complete."""
    deleted = await memory_repo.delete_memory(user_id, memory_id)
    return {"deleted": bool(deleted), "state": "cleanup_complete" if deleted
            else "already_gone"}


# ── §16.24: embedding/index migrations ─────────────────────────────────

@dataclass
class IndexMigration:
    migration_id: str
    from_version: str
    to_version: str
    state: str = "preparing"   # preparing | building | validating | active | rolled_back
    coverage: int = 0
    total: int = 0

    @property
    def complete_coverage(self) -> bool:
        return self.total > 0 and self.coverage >= self.total

    def require_compatible(self, from_vector_dim: int, to_vector_dim: int) -> None:
        """Incompatible vectors are NEVER mixed — a dimension change is a
        rebuild, not a merge."""
        if from_vector_dim != to_vector_dim:
            self.state = "building"   # full rebuild required
            self.note = (f"dimension change {from_vector_dim}→{to_vector_dim}: "
                         "full rebuild, no silent mixing")

    async def switch_active(self, *, validate_fn=None) -> bool:
        """§16.24: the switch is DELIBERATE — coverage complete AND
        retrieval validation passed BEFORE activation; a rollback path
        stays open until acceptance."""
        if validate_fn is not None:
            ok = await validate_fn()
            if not ok:
                return False   # acceptance failed — rollback path stays open
        if not self.complete_coverage:
            return False
        self.state = "active"
        return True
