"""Versioned prompt + routing-policy stores (Phase 13 §13.5).

Prompts and policies have VERSIONED ROLLBACK: rows are immutable, the
activation is a pointer per prompt id, and rollback moves the pointer —
so "baseline_ref"/"candidate_ref" in experiments name REAL versions and
a promotion rollback restores the PRIOR version exactly.

The routing policy's stats snapshot/restore through the same contract:
snapshots are durable rows; rollback writes the counters back into the
live repository. The learner never authorizes anything itself (§12.35).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from backend.models.admission import epoch_now


def new_version_id() -> str:
    return f"pv_{uuid.uuid4().hex[:20]}"


def new_snapshot_id() -> str:
    return f"psnap_{uuid.uuid4().hex[:20]}"


class PromptVersionStore:
    """SQLite reference implementation (Firestore parity follows the same
    contract as every Phase 13 store)."""

    def __init__(self, db):
        self.db = db

    async def checkout(self, prompt_id: str, template: str, *,
                       created_by: str, note: str = "") -> Dict[str, Any]:
        """Create the next immutable version. Identical content to the
        current active version short-circuits (no duplicate versions)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT version_id, version, template FROM prompt_versions "
            "WHERE prompt_id=? ORDER BY version DESC LIMIT 1", (prompt_id,))
        row = await cursor.fetchone()
        # Content-addressed: identical content to the LATEST version
        # returns that version (no duplicates) — regardless of activation.
        if row and row["template"] == template:
            return dict(row)
        next_version = int(row["version"] if row else 0) + 1
        version_id = new_version_id()
        await self.db.execute(
            "INSERT INTO prompt_versions "
            "(version_id, prompt_id, version, template, note, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version_id, prompt_id, next_version, template, note,
             created_by, now))
        await self.db.commit()
        return {"version_id": version_id, "prompt_id": prompt_id,
                "version": next_version, "template": template,
                "note": note, "created_by": created_by, "created_at": now}

    async def activate(self, prompt_id: str, version_id: str, *,
                       activated_by: str) -> Dict[str, Any]:
        """Move the activation pointer (rows are never rewritten)."""
        now = epoch_now()
        cursor = await self.db.execute(
            "SELECT version_id FROM prompt_versions WHERE version_id=? AND prompt_id=?",
            (version_id, prompt_id))
        if not await cursor.fetchone():
            raise ValueError(f"Unknown prompt version {version_id}")
        await self.db.execute(
            "INSERT INTO prompt_activations "
            "(prompt_id, active_version_id, activated_by, activated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(prompt_id) DO UPDATE SET "
            "active_version_id=excluded.active_version_id, "
            "activated_by=excluded.activated_by, "
            "activated_at=excluded.activated_at",
            (prompt_id, version_id, activated_by, now))
        await self.db.commit()
        return await self.get_active(prompt_id)

    async def get_active(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT v.version_id, v.prompt_id, v.version, v.template, "
            "v.note, v.created_by, v.created_at, a.activated_by, a.activated_at "
            "FROM prompt_activations a JOIN prompt_versions v "
            "ON v.version_id = a.active_version_id WHERE a.prompt_id=?",
            (prompt_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_by_version_id(self, version_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM prompt_versions WHERE version_id=?", (version_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_version(self, prompt_id: str, version: int) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id=? AND version=?",
            (prompt_id, version))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_versions(self, prompt_id: str,
                            limit: int = 50) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id=? "
            "ORDER BY version DESC LIMIT ?", (prompt_id, limit))
        return [dict(r) for r in await cursor.fetchall()]

    async def rollback_to(self, prompt_id: str, version: int, *,
                          operator: str) -> Dict[str, Any]:
        """§13 acceptance: rollback restores the prior version — the
        pointer moves back; history is preserved."""
        target = await self.get_version(prompt_id, version)
        if not target:
            raise ValueError(f"Unknown version {version} for {prompt_id}")
        return await self.activate(prompt_id, target["version_id"],
                                   activated_by=f"rollback:{operator}")


class PolicySnapshotStore:
    """Durable snapshots of the adaptive policy stats (§13.5: policies
    have versioned rollback)."""

    def __init__(self, db):
        self.db = db

    async def snapshot(self, stats: List[Dict[str, Any]], *,
                       created_by: str) -> Dict[str, Any]:
        import json as _json
        now = epoch_now()
        snapshot_id = new_snapshot_id()
        await self.db.execute(
            "INSERT INTO policy_snapshots "
            "(snapshot_id, stats_json, created_by, created_at) "
            "VALUES (?, ?, ?, ?)",
            (snapshot_id, _json.dumps(stats or []), created_by, now))
        await self.db.commit()
        return {"snapshot_id": snapshot_id, "created_by": created_by,
                "created_at": now, "stats": list(stats or [])}

    async def latest(self) -> Optional[Dict[str, Any]]:
        import json as _json
        cursor = await self.db.execute(
            "SELECT * FROM policy_snapshots ORDER BY created_at DESC LIMIT 1")
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["stats"] = _json.loads(d.pop("stats_json") or "[]")
        return d


# ── §14.25: the policy ARTIFACT — the full learning state ──────────────

ARTIFACT_KIND = "policy_artifact_v1"


async def capture_policy_artifact(stats_repo, snapshot_store, *,
                                  created_by: str) -> Dict[str, Any]:
    """Capture the COMPLETE learned state — global stats AND per-context
    bandit stats — as one durable artifact. Rollback (§13.5) restores the
    policy exactly, not just the leaderboard."""
    stats = await stats_repo.get_stats()
    context_rows = (await stats_repo.all_context_stats()
                    if hasattr(stats_repo, "all_context_stats") else [])
    snap = await snapshot_store.snapshot(
        [{"kind": ARTIFACT_KIND,
          "global": stats,
          "context": context_rows}],
        created_by=created_by)
    return snap


async def restore_policy_artifact(stats_repo, artifact: Dict[str, Any])         -> Dict[str, int]:
    """Restore a captured artifact: global stats first, then the
    per-context rows. Returns the restore counts (auditable)."""
    entries = artifact.get("stats") or []
    restored = {"global": 0, "context": 0}
    for entry in entries:
        if entry.get("kind") != ARTIFACT_KIND:
            continue   # not a policy artifact row
        if hasattr(stats_repo, "restore_snapshot"):
            restored["global"] = await stats_repo.restore_snapshot(
                entry.get("global") or [])
        if hasattr(stats_repo, "restore_context_snapshot"):
            restored["context"] = await stats_repo.restore_context_snapshot(
                entry.get("context") or [])
    return restored


_store: Optional[PromptVersionStore] = None
_snapshots: Optional[PolicySnapshotStore] = None


def set_prompt_version_store(store) -> None:
    global _store
    _store = store


def get_prompt_version_store() -> Optional[PromptVersionStore]:
    return _store


def set_policy_snapshot_store(store) -> None:
    global _snapshots
    _snapshots = store


def get_policy_snapshot_store() -> Optional[PolicySnapshotStore]:
    return _snapshots
