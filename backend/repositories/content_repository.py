"""Durable public content and private contact submissions."""
import json
import uuid
from datetime import datetime, timezone


class SQLiteContentRepository:
    def __init__(self, db):
        self.db = db

    async def create(self, kind: str, payload: dict) -> dict:
        record = {**payload, "id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}
        await self.db.execute("INSERT INTO content_records (id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                              (record["id"], kind, json.dumps(record), record["created_at"]))
        await self.db.commit()
        return record

    async def list_public_posts(self, limit: int = 100) -> list:
        rows = await self.db.fetch_all("SELECT payload FROM content_records WHERE kind = 'blog' ORDER BY created_at DESC LIMIT ?", (limit,))
        return [json.loads(row["payload"]) for row in rows]


class FirestoreContentRepository:
    async def create(self, kind: str, payload: dict) -> dict:
        from backend.repositories.firestore.database import FirestoreDB
        db = await FirestoreDB.get_client()
        record = {**payload, "id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat()}
        await db.collection("public_posts" if kind == "blog" else "contact_submissions").document(record["id"]).create(record)
        return record

    async def list_public_posts(self, limit: int = 100) -> list:
        from backend.repositories.firestore.database import FirestoreDB
        db = await FirestoreDB.get_client()
        return [doc.to_dict() async for doc in db.collection("public_posts").order_by("created_at", direction="DESCENDING").limit(limit).stream()]
