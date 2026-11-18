"""Park and wake workflow tasks using the Firestore command ledger."""
import hashlib
import json
from datetime import datetime, timezone

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from backend.models.admission import epoch_now
from backend.models.command import new_wait_id
from backend.repositories.firestore.database import FirestoreDB


def open_wait_ref(db, run_id, task_id):
    identity = json.dumps([run_id, task_id], separators=(",", ":")).encode()
    return db.collection("parent_wait_slots").document(hashlib.sha256(identity).hexdigest())


class FirestoreWaitRepository:
    async def create_wait(self, parent_run_id, parent_task_id, child_run_id=None,
                          child_command_id=None, condition="child_build_succeeded", parent_command_id=None):
        db = await FirestoreDB.get_client()
        wait_id = new_wait_id()
        record = {"wait_id": wait_id, "parent_run_id": parent_run_id, "parent_task_id": parent_task_id,
                  "parent_command_id": parent_command_id, "child_run_id": child_run_id,
                  "child_command_id": child_command_id, "condition": condition,
                  "state": "waiting", "created_at": datetime.now(timezone.utc).isoformat()}
        existing = await self.find_open_wait(parent_run_id, parent_task_id)
        if existing:
            return existing["wait_id"]
        batch = db.batch()
        batch.create(open_wait_ref(db, parent_run_id, parent_task_id), {"wait_id": wait_id})
        batch.create(db.collection("parent_waits").document(wait_id), record)
        try:
            await batch.commit()
        except AlreadyExists:
            existing = await self.find_open_wait(parent_run_id, parent_task_id)
            if existing:
                return existing["wait_id"]
            raise
        return wait_id

    async def _parent_query(self, run_id, task_id):
        db = await FirestoreDB.get_client()
        return (db.collection("parent_waits").where(filter=FieldFilter("parent_run_id", "==", run_id))
                .where(filter=FieldFilter("parent_task_id", "==", task_id)))

    async def find_open_wait(self, parent_run_id, parent_task_id):
        query = await self._parent_query(parent_run_id, parent_task_id)
        docs = await query.where(filter=FieldFilter("state", "==", "waiting")).limit(1).get()
        return docs[0].to_dict() if docs else None

    async def latest_wait(self, run_id, task_id):
        query = await self._parent_query(run_id, task_id)
        docs = await query.order_by("created_at", direction="DESCENDING").limit(1).get()
        return docs[0].to_dict() if docs else None

    async def unresolved_waits(self, limit=50):
        db = await FirestoreDB.get_client()
        query = db.collection("parent_waits").where(filter=FieldFilter("state", "==", "waiting"))
        return [doc.to_dict() for doc in await query.limit(limit).get()]

    async def unresolved_waits_for_child(self, child_run_id):
        db = await FirestoreDB.get_client()
        query = (db.collection("parent_waits").where(filter=FieldFilter("child_run_id", "==", child_run_id))
                 .where(filter=FieldFilter("state", "==", "waiting")))
        return [doc.to_dict() for doc in await query.get()]

    async def resolve_with_wake(self, wait, payload, *, outcome="resolved"):
        if outcome not in ("resolved", "failed"):
            raise ValueError("Unsupported parent-wait outcome")
        return await self._resolve(wait["wait_id"], outcome, None, payload)

    async def resolve_wait(self, wait_id, outcome, by_event_id=None):
        return await self._resolve(wait_id, outcome, by_event_id, None)

    async def _resolve(self, wait_id, outcome, by_event_id, wake):
        db = await FirestoreDB.get_client()
        ref = db.collection("parent_waits").document(wait_id)
        now = epoch_now()

        @firestore.async_transactional
        async def resolve(transaction):
            wait = (await ref.get(transaction=transaction)).to_dict() or {}
            if wait.get("state") != "waiting":
                return False
            slot_ref = open_wait_ref(db, wait["parent_run_id"], wait["parent_task_id"])
            slot = (await slot_ref.get(transaction=transaction)).to_dict() or {}
            if slot.get("wait_id") == wait_id:
                transaction.delete(slot_ref)
            transaction.update(ref, {"state": outcome, "resolved_at": now, "resolved_by_event_id": by_event_id})
            if wake is not None:
                transaction.create(db.collection("outbox_events").document("wake-" + wait_id), {
                    "event_type": "parent.ready", "aggregate_id": wait["parent_run_id"],
                    "aggregate_version": 1, "schema_version": 1, "payload_json": json.dumps(wake),
                    "created_at": now, "available_at": now, "published_at": None, "publish_attempts": 0,
                    "dispatcher_lease_expires_at": None, "dispatcher_owner": None})
            return True

        return await resolve(db.transaction())
