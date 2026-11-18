from abc import ABC, abstractmethod
from typing import List, Optional
from backend.models.plugin import PluginRecord
from datetime import datetime, timezone

class PluginRepository(ABC):
    @abstractmethod
    async def save(self, record: PluginRecord) -> None:
        pass
        
    @abstractmethod
    async def get_by_id(self, record_id: str) -> Optional[PluginRecord]:
        pass
        
    @abstractmethod
    async def list_all(self, user_id: str = None) -> List[PluginRecord]:
        pass

class InMemoryPluginRepository(PluginRepository):
    def __init__(self):
        self._store = {}
        
    async def save(self, record: PluginRecord) -> None:
        record.updated_at = datetime.now(timezone.utc)
        self._store[record.id] = record
        
    async def get_by_id(self, record_id: str) -> Optional[PluginRecord]:
        return self._store.get(record_id)
        
    async def list_all(self, user_id: str = None) -> List[PluginRecord]:
        # For simplicity, if user_id is provided, we filter by it or global
        # Scope enforcement is largely handled by policy, but we can do basic filtering here
        results = []
        for r in self._store.values():
            if user_id and r.manifest.scope.value == "USER" and r.installed_by != user_id:
                continue
            results.append(r)
        return results

class FirestorePluginRepository(PluginRepository):
    def __init__(self, db_client):
        self.db = db_client
        self.collection_name = "plugins"
        
    async def save(self, record: PluginRecord) -> None:
        import asyncio
        record.updated_at = datetime.now(timezone.utc)
        doc_ref = self.db.collection(self.collection_name).document(record.id)
        await asyncio.to_thread(doc_ref.set, record.model_dump(mode="json"))
        
    async def get_by_id(self, record_id: str) -> Optional[PluginRecord]:
        import asyncio
        doc_ref = self.db.collection(self.collection_name).document(record_id)
        doc = await asyncio.to_thread(doc_ref.get)
        if not doc.exists:
            return None
        return PluginRecord.model_validate(doc.to_dict())
        
    async def list_all(self, user_id: str = None) -> List[PluginRecord]:
        import asyncio
        collection = self.db.collection(self.collection_name)
        docs = await asyncio.to_thread(lambda: list(collection.stream()))
        results = []
        for doc in docs:
            r = PluginRecord.model_validate(doc.to_dict())
            if user_id and r.manifest.scope.value == "USER" and r.installed_by != user_id:
                continue
            results.append(r)
        return results


class SQLitePluginRepository(PluginRepository):
    def __init__(self, db):
        self.db = db

    async def save(self, record: PluginRecord) -> None:
        record.updated_at = datetime.now(timezone.utc)
        await self.db.execute(
            """INSERT INTO plugins (id, manifest_json, state, installed_by, installed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET manifest_json=excluded.manifest_json,
            state=excluded.state, updated_at=excluded.updated_at""",
            (record.id, record.manifest.model_dump_json(), record.state.value,
             record.installed_by, record.installed_at.isoformat(), record.updated_at.isoformat()),
        )
        await self.db.commit()

    async def get_by_id(self, record_id: str) -> Optional[PluginRecord]:
        import json
        row = await self.db.fetch_one("SELECT * FROM plugins WHERE id=?", (record_id,))
        if not row:
            return None
        return PluginRecord(id=row['id'], manifest=json.loads(row['manifest_json']), state=row['state'],
                            installed_by=row['installed_by'], installed_at=row['installed_at'], updated_at=row['updated_at'])

    async def list_all(self, user_id: str = None) -> List[PluginRecord]:
        import json
        rows = await self.db.fetch_all("SELECT * FROM plugins ORDER BY installed_at")
        records = [PluginRecord(id=row['id'], manifest=json.loads(row['manifest_json']), state=row['state'],
                                installed_by=row['installed_by'], installed_at=row['installed_at'], updated_at=row['updated_at']) for row in rows]
        return [r for r in records if not user_id or r.manifest.scope.value != 'USER' or r.installed_by == user_id]


class AsyncFirestorePluginRepository(PluginRepository):
    def __init__(self, db):
        self.db = db
        self.collection_name = "plugins"

    async def save(self, record: PluginRecord) -> None:
        record.updated_at = datetime.now(timezone.utc)
        await self.db.collection(self.collection_name).document(record.id).set(record.model_dump(mode='json'))

    async def get_by_id(self, record_id: str) -> Optional[PluginRecord]:
        doc = await self.db.collection(self.collection_name).document(record_id).get()
        return PluginRecord.model_validate(doc.to_dict()) if doc.exists else None

    async def list_all(self, user_id: str = None) -> List[PluginRecord]:
        results = []
        async for doc in self.db.collection(self.collection_name).stream():
            record = PluginRecord.model_validate(doc.to_dict())
            if not user_id or record.manifest.scope.value != 'USER' or record.installed_by == user_id:
                results.append(record)
        return results
