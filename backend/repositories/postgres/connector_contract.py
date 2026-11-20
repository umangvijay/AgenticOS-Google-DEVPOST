"""Durable connector revision, validation and activation contract on PostgreSQL."""
import json
import uuid


def payload(value):
    return json.loads(value) if isinstance(value, str) else dict(value)


class PostgresConnectorContract:
    async def save_revision(self, mcp_id, revision):
        await self.db.execute("INSERT INTO pg_mcp_revisions(mcp_id,revision_id,payload) VALUES($1,$2,$3::jsonb) "
                              "ON CONFLICT DO NOTHING", mcp_id, revision["revision_id"], json.dumps(revision))

    async def get_revision(self, mcp_id, revision_id):
        row = await self.db.fetch_one("SELECT payload FROM pg_mcp_revisions WHERE mcp_id=$1 AND revision_id=$2",
                                      mcp_id, revision_id)
        return payload(row["payload"]) if row else None

    async def list_revisions(self, mcp_id):
        rows = await self.db.fetch_all("SELECT payload FROM pg_mcp_revisions WHERE mcp_id=$1 "
                                       "ORDER BY payload->>'created_at' DESC LIMIT 100", mcp_id)
        return [payload(row["payload"]) for row in rows]

    async def save_validation_run(self, connector_id, run):
        await self.db.execute("INSERT INTO pg_connector_validation_runs(validation_run_id,connector_id,payload) "
                              "VALUES($1,$2,$3::jsonb) ON CONFLICT(validation_run_id) DO UPDATE SET "
                              "payload=EXCLUDED.payload WHERE pg_connector_validation_runs.connector_id=EXCLUDED.connector_id",
                              run["validation_run_id"], connector_id, json.dumps(run))

    async def list_validation_runs(self, connector_id, limit=100):
        rows = await self.db.fetch_all("SELECT payload FROM pg_connector_validation_runs WHERE connector_id=$1 "
                                       "ORDER BY (payload->>'started_at')::bigint DESC LIMIT $2", connector_id, limit)
        return [payload(row["payload"]) for row in rows]

    async def activate_revision_atomic(self, mcp_id, revision_id, expected, actor, *, browser_ready=False):
        from backend.services.connector_activation import ActivationConflictError
        async with self.db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT active_revision_id FROM pg_mcps WHERE id=$1 FOR UPDATE", mcp_id)
            if not row or not await conn.fetchval("SELECT 1 FROM pg_mcp_revisions WHERE mcp_id=$1 AND revision_id=$2",
                                                 mcp_id, revision_id):
                raise ValueError("Candidate revision not found for connector")
            current = row["active_revision_id"]
            if expected is not None and (current or "") != expected:
                raise ActivationConflictError("Active revision changed")
            if browser_ready:
                await conn.execute("UPDATE pg_mcps SET trust_tier='browser_ready' WHERE id=$1", mcp_id)
            await conn.execute("UPDATE pg_mcps SET active_revision_id=$2,updated_at=now() WHERE id=$1", mcp_id, revision_id)
            await conn.execute("INSERT INTO pg_connector_activations(activation_id,mcp_id,revision_id,previous_revision_id,actor) "
                               "VALUES($1,$2,$3,$4,$5)", "act_" + uuid.uuid4().hex, mcp_id, revision_id, current, actor)
        return {"activated": True, "revision_id": revision_id, "previous_revision_id": current}
