"""Firestore prompt-version + policy-snapshot stores (Phase 13 §13.5).

The SAME contract as the SQLite PromptVersionStore / PolicySnapshotStore:
immutable versions with a per-prompt activation pointer (rollback moves
the pointer), and durable policy snapshots.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

logger = logging.getLogger(__name__)

VERSIONS = "prompt_versions"
ACTIVATIONS = "prompt_activations"
SNAPSHOTS = "policy_snapshots"


def _new_version_id() -> str:
    return f"pv_{uuid.uuid4().hex[:20]}"


def _new_snapshot_id() -> str:
    return f"psnap_{uuid.uuid4().hex[:20]}"


class FirestorePromptVersionStore:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def checkout(self, prompt_id: str, template: str, *,
                       created_by: str, note: str = "") -> Dict[str, Any]:
        now = epoch_now()
        db = await self._get_db()
        query = (db.collection(VERSIONS).where("prompt_id", "==", prompt_id)
                 .limit(500))
        max_version = 0
        async for snap in query.stream():
            max_version = max(
                max_version,
                int((snap.to_dict() or {}).get("version") or 0))
        active = await self.get_active(prompt_id)
        if active and active["template"] == template:
            return active
        version_id = _new_version_id()
        doc = {"prompt_id": prompt_id, "version": max_version + 1,
               "template": template, "note": note, "created_by": created_by,
               "created_at": now}
        await db.collection(VERSIONS).document(version_id).create(doc)
        out = dict(doc)
        out["version_id"] = version_id
        return out

    async def activate(self, prompt_id: str, version_id: str, *,
                       activated_by: str) -> Dict[str, Any]:
        db = await self._get_db()
        snap = await db.collection(VERSIONS).document(version_id).get()
        if not snap.exists:
            raise ValueError(f"Unknown prompt version {version_id}")
        # §13.21: OWNERSHIP — the version must belong to the requested
        # prompt; a foreign version_id can never be activated under
        # another prompt's activation pointer.
        if (snap.to_dict() or {}).get("prompt_id") != prompt_id:
            raise ValueError(
                f"Version {version_id} does not belong to prompt {prompt_id}")
        now = epoch_now()
        await db.collection(ACTIVATIONS).document(prompt_id).set({
            "prompt_id": prompt_id, "active_version_id": version_id,
            "activated_by": activated_by, "activated_at": now,
        }, merge=False)
        return await self.get_active(prompt_id)

    async def get_active(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        act = await db.collection(ACTIVATIONS).document(prompt_id).get()
        if not act.exists:
            return None
        version_id = (act.to_dict() or {}).get("active_version_id")
        if not version_id:
            return None
        snap = await db.collection(VERSIONS).document(version_id).get()
        if not snap.exists:
            return None
        d = dict(snap.to_dict() or {})
        d["version_id"] = version_id
        d["activated_by"] = (act.to_dict() or {}).get("activated_by")
        return d

    async def get_by_version_id(self, version_id: str) -> Optional[Dict[str, Any]]:
        snap = await (await self._get_db()).collection(VERSIONS).document(
            version_id).get()
        if not snap.exists:
            return None
        d = dict(snap.to_dict() or {})
        d["version_id"] = version_id
        return d

    async def get_version(self, prompt_id: str, version: int) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        query = (db.collection(VERSIONS).where("prompt_id", "==", prompt_id)
                 .where("version", "==", version).limit(1))
        async for snap in query.stream():
            d = dict(snap.to_dict() or {})
            d["version_id"] = snap.id
            return d
        return None

    async def list_versions(self, prompt_id: str,
                            limit: int = 50) -> List[Dict[str, Any]]:
        db = await self._get_db()
        query = (db.collection(VERSIONS).where("prompt_id", "==", prompt_id)
                 .limit(limit))
        rows = []
        async for snap in query.stream():
            d = dict(snap.to_dict() or {})
            d["version_id"] = snap.id
            rows.append(d)
        rows.sort(key=lambda r: r.get("version", 0), reverse=True)
        return rows

    async def rollback_to(self, prompt_id: str, version: int, *,
                          operator: str) -> Dict[str, Any]:
        target = await self.get_version(prompt_id, version)
        if not target:
            raise ValueError(f"Unknown version {version} for {prompt_id}")
        return await self.activate(prompt_id, target["version_id"],
                                   activated_by=f"rollback:{operator}")


class FirestorePolicySnapshotStore:
    def __init__(self):
        self._db = None

    async def _get_db(self):
        if self._db is None:
            from backend.repositories.firestore.database import FirestoreDB
            self._db = await FirestoreDB.get_client()
        return self._db

    async def snapshot(self, stats: List[Dict[str, Any]], *,
                       created_by: str) -> Dict[str, Any]:
        now = epoch_now()
        snapshot_id = _new_snapshot_id()
        await (await self._get_db()).collection(SNAPSHOTS).document(
            snapshot_id).set({
                "stats": list(stats or []), "created_by": created_by,
                "created_at": now})
        return {"snapshot_id": snapshot_id, "created_by": created_by,
                "created_at": now, "stats": list(stats or [])}

    async def latest(self) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        best = None
        async for snap in db.collection(SNAPSHOTS).limit(100).stream():
            d = dict(snap.to_dict() or {})
            d["snapshot_id"] = snap.id
            if best is None or (d.get("created_at") or 0) > (best.get("created_at") or 0):
                best = d
        return best
