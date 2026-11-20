from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from backend.repositories.base import BaseMCPRepository
from backend.repositories.firestore.database import FirestoreDB


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(value)


class FirestoreMCPRepository(BaseMCPRepository):
    """Firestore implementation of BaseMCPRepository."""

    async def _get_db(self):
        return await FirestoreDB.get_client()

    async def register_mcp(self, manifest_data) -> None:
        payload = _as_dict(manifest_data)
        mcp_id = payload.get("mcp_id")
        if not mcp_id:
            raise ValueError("MCP manifest must have 'mcp_id'")
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        payload.setdefault("is_enabled", False)
        db = await self._get_db()
        from google.cloud import firestore
        ref = db.collection("mcps").document(mcp_id)

        @firestore.async_transactional
        async def register(transaction):
            previous = (await ref.get(transaction=transaction)).to_dict()
            if previous is not None and previous.get("owner") != payload.get("owner"):
                raise PermissionError("Connector belongs to another owner")
            transaction.set(ref, payload, merge=True)
        await register(db.transaction())

    async def get_mcp(self, mcp_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        doc = await db.collection("mcps").document(mcp_id).get()
        if doc.exists:
            return doc.to_dict() or {}
        return None

    async def list_mcps(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        db = await self._get_db()
        mcps = []
        async for doc in db.collection("mcps").stream():
            data = doc.to_dict() or {}
            owner = data.get("owner")
            if user_id and owner not in (user_id, "system"):
                continue
            mcps.append(data)
        return mcps

    async def update_mcp_health(self, mcp_id: str, status: str, timestamp: datetime) -> None:
        db = await self._get_db()
        ts = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        await db.collection("mcps").document(mcp_id).update(
            {
                "health": status,
                "health_updated_at": ts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def list_public_mcps(self) -> List[Dict[str, Any]]:
        db = await self._get_db()
        # A single equality filter works without an additional composite index.
        public = []
        async for doc in db.collection("mcps").where("is_public", "==", True).stream():
            data = doc.to_dict() or {}
            if data.get("is_public") is True:
                public.append(data)
        return sorted(public, key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    async def set_public(self, mcp_id: str, is_public: bool) -> bool:
        from google.api_core.exceptions import NotFound
        db = await self._get_db()
        try:
            await db.collection("mcps").document(mcp_id).update({
                "is_public": bool(is_public),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except NotFound:
            return False
        return True

    async def set_mcp_enabled(self, mcp_id: str, enabled: bool) -> None:
        db = await self._get_db()
        from google.cloud import firestore
        await db.collection("mcps").document(mcp_id).update(
            {
                "is_enabled": bool(enabled),
                "is_active": bool(enabled),
                "metadata.pending_publication_token": firestore.DELETE_FIELD,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def publish_managed_candidate(self, mcp_id, *, owner, revision_id, publication_token, metadata):
        from google.cloud import firestore
        db = await self._get_db()
        ref = db.collection("mcps").document(mcp_id)

        @firestore.async_transactional
        async def publish(transaction):
            current = (await ref.get(transaction=transaction)).to_dict()
            if (not current or current.get("owner") != owner
                    or current.get("active_revision_id") != revision_id
                    or current.get("is_enabled")
                    or (current.get("metadata") or {}).get("pending_publication_token") != publication_token):
                return False
            transaction.update(ref, {"is_enabled": True, "is_active": True, "trust_tier": "verified",
                                     "metadata": metadata, "updated_at": datetime.now(timezone.utc).isoformat()})
            return True
        return await publish(db.transaction())

    async def update_mcp_state(self, mcp_id: str, state: str) -> None:
        db = await self._get_db()
        await db.collection("mcps").document(mcp_id).update(
            {
                "state": state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def update_mcp_auth(self, mcp_id: str, auth: Dict[str, Any]) -> None:
        db = await self._get_db()
        await db.collection("mcps").document(mcp_id).update(
            {
                "auth": auth,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def delete_mcp(self, mcp_id: str) -> bool:
        db = await self._get_db()
        ref = db.collection("mcps").document(mcp_id)
        snap = await ref.get()
        if not snap.exists:
            return False
        async for revision in ref.collection("revisions").stream():
            await revision.reference.delete()
        await ref.delete()
        async for doc in db.collection("mcp_tools").where("mcp_id", "==", mcp_id).stream():
            await doc.reference.delete()
        return True

    async def cache_tools(self, mcp_id: str, tools: List[Any] = None, *, replace_all: bool = True) -> None:
        if isinstance(mcp_id, list) and tools is None:
            tools = mcp_id
            mcp_id = None
            if tools:
                first = _as_dict(tools[0])
                mcp_id = first.get("mcp_id")
        tools = tools or []
        db = await self._get_db()
        if mcp_id and replace_all:
            async for doc in db.collection("mcp_tools").where("mcp_id", "==", mcp_id).stream():
                await doc.reference.delete()
        if not tools:
            return
        batch = db.batch()
        for t in tools:
            payload = _as_dict(t)
            payload.setdefault("mcp_id", mcp_id)
            tool_name = payload.get("tool_name") or payload.get("name") or "unknown"
            payload["tool_name"] = tool_name
            doc_id = f"{payload.get('mcp_id')}_{tool_name}"
            ref = db.collection("mcp_tools").document(doc_id)
            batch.set(ref, payload)
        await batch.commit()

    async def get_cached_tools(self, mcp_id: Optional[str] = None) -> List[Dict[str, Any]]:
        db = await self._get_db()
        query = db.collection("mcp_tools")
        if mcp_id:
            query = query.where("mcp_id", "==", mcp_id)
        tools = []
        async for doc in query.stream():
            tools.append(doc.to_dict() or {})
        return tools

    async def create_build(self, build: Dict[str, Any]) -> None:
        payload = dict(build)
        now = datetime.now(timezone.utc).isoformat()
        payload.setdefault("logs", [])
        payload.setdefault("tools", [])
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        build_id = payload.get("build_id")
        if not build_id:
            raise ValueError("Build dict must have 'build_id'")
        db = await self._get_db()
        await db.collection("mcp_builds").document(build_id).set(payload)

    async def update_build(self, build_id: str, updates: Dict[str, Any]) -> None:
        if not updates:
            return
        payload = dict(updates)
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        db = await self._get_db()
        await db.collection("mcp_builds").document(build_id).update(payload)

    async def get_build(self, build_id: str) -> Optional[Dict[str, Any]]:
        db = await self._get_db()
        doc = await db.collection("mcp_builds").document(build_id).get()
        if doc.exists:
            return doc.to_dict() or {}
        return None

    async def save_revision(self, mcp_id: str, revision: dict) -> None:
        from google.api_core.exceptions import AlreadyExists
        db = await self._get_db()
        ref = db.collection("mcps").document(mcp_id).collection("revisions").document(revision["revision_id"])
        try:
            await ref.create(revision)
        except AlreadyExists:
            pass

    async def list_revisions(self, mcp_id: str) -> list:
        db = await self._get_db()
        query = db.collection("mcps").document(mcp_id).collection("revisions").order_by("created_at", direction="DESCENDING").limit(100)
        return [doc.to_dict() async for doc in query.stream()]

    async def get_revision(self, mcp_id: str, revision_id: str):
        db = await self._get_db()
        doc = await db.collection("mcps").document(mcp_id).collection("revisions").document(revision_id).get()
        return doc.to_dict() if doc.exists else None

    async def save_validation_run(self, connector_id: str, run: dict) -> None:
        db = await self._get_db()
        await db.collection("mcps").document(connector_id).collection("validation_runs").document(
            run["validation_run_id"]).set(run)

    async def list_validation_runs(self, connector_id: str, limit: int = 100) -> list:
        db = await self._get_db()
        query = db.collection("mcps").document(connector_id).collection("validation_runs").order_by(
            "started_at", direction="DESCENDING").limit(limit)
        return [doc.to_dict() async for doc in query.stream()]

    async def activate_revision_atomic(self, mcp_id, revision_id, expected, actor, *, browser_ready=False):
        from google.cloud import firestore
        from backend.services.connector_activation import ActivationConflictError
        import uuid
        db = await self._get_db()
        ref = db.collection("mcps").document(mcp_id)
        revision_ref = ref.collection("revisions").document(revision_id)
        activation_ref = ref.collection("activations").document("act_" + uuid.uuid4().hex)

        @firestore.async_transactional
        async def activate(transaction):
            current_doc = await ref.get(transaction=transaction)
            revision_doc = await revision_ref.get(transaction=transaction)
            if not current_doc.exists or not revision_doc.exists:
                raise ValueError("Candidate revision not found for connector")
            previous = (current_doc.to_dict() or {}).get("active_revision_id")
            if expected is not None and (previous or "") != expected:
                raise ActivationConflictError("Active revision changed")
            fields = {"active_revision_id": revision_id}
            if browser_ready:
                fields["trust_tier"] = "browser_ready"
            transaction.update(ref, fields)
            transaction.create(activation_ref, {"revision_id": revision_id, "previous_revision_id": previous,
                                                "actor": actor, "created_at": datetime.now(timezone.utc).isoformat()})
            return {"activated": True, "revision_id": revision_id, "previous_revision_id": previous}
        return await activate(db.transaction())
