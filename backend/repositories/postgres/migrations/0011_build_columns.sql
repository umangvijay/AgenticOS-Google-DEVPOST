-- §21.3: the durable build row carries the waiter + credential refs.
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS wait_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS credential_reference TEXT;
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS required_operations_json TEXT NOT NULL DEFAULT '[]';
