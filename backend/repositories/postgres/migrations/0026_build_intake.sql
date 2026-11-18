-- §29.14/§29.15 revision: the durable build row carries the application-
-- intake record and the Forge product identity (additive).
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS intake_json TEXT NOT NULL DEFAULT '{}';
