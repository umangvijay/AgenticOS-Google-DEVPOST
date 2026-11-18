"""§28.9 — durable, bounded, cancellable modeling jobs.

The synchronous export path produces verified geometry in milliseconds,
but the PRODUCT contract the workspace needs is: a job record that
survives restart, a RUNNING state the UI can show, and a cancellation
that lands between stages so a cancelled job never publishes. Jobs are
persisted as small JSON records beside the artifacts they produce.

States: queued → running → ready | failed | cancelled
A cancelled job leaves NO artifact: publication only happens on the
ready path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("modeling_jobs")

JOB_TTL_S = 3600
VALID_TRANSITIONS = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"ready", "failed", "cancelled"},
    "ready": set(), "failed": set(), "cancelled": set(),
}


class ModelingJobError(ValueError):
    pass


class ModelingJobStore:
    """File-backed job records: bounded, restart-readable, and cheap."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.root / f"job-{job_id}.json"

    def create(self, owner_user_id: str, parameters: Dict[str, Any],
               run_id: Optional[str] = None, *,
               parent_job_id: Optional[str] = None,
               version: int = 1) -> Dict[str, Any]:
        job_id = f"mj-{uuid.uuid4().hex[:12]}"
        record = {
            "job_id": job_id,
            "owner_user_id": owner_user_id,
            "parameters": parameters,
            "run_id": run_id,
            # 30.40: versioned edits — an edit is a CHILD job one version
            # above its parent; every revision keeps its own artifact.
            "parent_job_id": parent_job_id,
            "version": int(version),
            "state": "queued",
            "artifact_id": None,
            "digest": None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "error": None,
        }
        self._write(record)
        return record

    def lineage(self, job_id: str) -> list:
        """The job's version chain, oldest first (walks parent links)."""
        chain = []
        current = self.load(job_id)
        while current is not None:
            chain.append(current)
            parent = current.get("parent_job_id")
            current = self.load(parent) if parent else None
        chain.reverse()
        return chain

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def transition(self, job_id: str, to_state: str, **fields) -> Dict[str, Any]:
        record = self.load(job_id)
        if record is None:
            raise ModelingJobError(f"unknown job {job_id}")
        current = record["state"]
        if to_state not in VALID_TRANSITIONS.get(current, set()):
            raise ModelingJobError(
                f"job {job_id}: illegal transition {current} → {to_state}")
        record["state"] = to_state
        record["updated_at"] = time.time()
        for key, value in fields.items():
            record[key] = value
        self._write(record)
        return record

    def _write(self, record: Dict[str, Any]) -> None:
        path = self._path(record["job_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2))
        tmp.replace(path)

    def sweep_expired(self) -> int:
        """Bounded sweep: terminal jobs older than the TTL are removed so
        the job directory cannot grow without bound."""
        removed = 0
        now = time.time()
        for path in self.root.glob("job-*.json"):
            try:
                record = json.loads(path.read_text())
                if (record.get("state") in ("ready", "failed", "cancelled")
                        and now - record.get("updated_at", 0) > JOB_TTL_S):
                    path.unlink()
                    removed += 1
            except (OSError, ValueError):
                continue
        return removed


_store: Optional[ModelingJobStore] = None
_tasks: Dict[str, asyncio.Task] = {}


def get_modeling_job_store() -> ModelingJobStore:
    global _store
    if _store is None:
        from backend.services.artifact_builder import artifacts_root
        _store = ModelingJobStore(artifacts_root() / "modeling-jobs")
    return _store


def reset_modeling_job_store() -> None:
    global _store
    _store = None
    _tasks.clear()


async def run_modeling_job(job_id: str, *, publish) -> Dict[str, Any]:
    """Execute one job under the durable contract:
    queued → running → (validate bytes → publish) → ready.
    Cancellation between stages leaves the job cancelled with NO
    artifact published. The `publish` callable does the generation +
    validation + save and returns the artifact metadata."""
    store = get_modeling_job_store()
    store.transition(job_id, "running")
    task = asyncio.current_task()
    if task is not None:
        _tasks[job_id] = task
    try:
        metadata = await publish()
        store.transition(job_id, "ready",
                         artifact_id=metadata.get("artifact_id"),
                         digest=metadata.get("checksums", {}).get(
                             next((f["path"] for f in metadata.get("files", [])
                                   if f["path"].endswith(".obj")), ""),
                             None) if metadata.get("checksums") else None)
        return metadata
    except asyncio.CancelledError:
        store.transition(job_id, "cancelled", error="cancelled before publication")
        raise
    except Exception as exc:  # noqa: BLE001 — the job record carries the failure
        store.transition(job_id, "failed", error=str(exc)[:300])
        raise
    finally:
        _tasks.pop(job_id, None)


async def cancel_modeling_job(job_id: str) -> Dict[str, Any]:
    store = get_modeling_job_store()
    record = store.load(job_id)
    if record is None:
        raise ModelingJobError(f"unknown job {job_id}")
    if record["state"] in ("ready", "failed", "cancelled"):
        return record
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — the failure is already recorded
            pass
    try:
        return store.transition(job_id, "cancelled", error="cancelled by operator")
    except ModelingJobError:
        return store.load(job_id) or record
