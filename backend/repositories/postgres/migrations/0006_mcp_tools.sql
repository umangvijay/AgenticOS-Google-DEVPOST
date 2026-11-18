-- §21.3: the mcp_tools catalog cache (per-tool rows the ToolRouter
-- reads) and richer build rows mirroring the SQLite schema.
CREATE TABLE IF NOT EXISTS mcp_tools (
    id BIGSERIAL PRIMARY KEY,
    tool_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    input_schema TEXT NOT NULL DEFAULT '{}',
    mcp_id TEXT NOT NULL,
    mcp_version TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    auth_requirements TEXT NOT NULL DEFAULT '[]',
    risk_level INTEGER NOT NULL DEFAULT 4
);
CREATE INDEX IF NOT EXISTS idx_mcp_tools_mcp ON mcp_tools(mcp_id);
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT '';
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS logs_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS tools_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS parent_workflow_id TEXT;
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS parent_task_id TEXT;
ALTER TABLE pg_mcps ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT '';
ALTER TABLE pg_mcps ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE pg_mcps RENAME COLUMN metadata TO legacy_metadata;
ALTER TABLE pg_mcps ADD COLUMN IF NOT EXISTS metadata TEXT NOT NULL DEFAULT '{}';
ALTER TABLE pg_mcp_builds ADD COLUMN IF NOT EXISTS build_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_mcp_builds_bid ON pg_mcp_builds(build_id) WHERE build_id IS NOT NULL;
