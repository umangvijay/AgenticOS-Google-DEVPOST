-- Preserve generated operation definitions and revision/validation history.
ALTER TABLE mcp_tools ADD COLUMN IF NOT EXISTS operation_json JSONB NOT NULL DEFAULT '{}';
ALTER TABLE pg_mcps ADD COLUMN IF NOT EXISTS contract_metadata JSONB NOT NULL DEFAULT '{}';
ALTER TABLE pg_mcps ADD COLUMN IF NOT EXISTS active_revision_id TEXT;
CREATE TABLE IF NOT EXISTS pg_mcp_revisions (
    mcp_id TEXT NOT NULL REFERENCES pg_mcps(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (mcp_id, revision_id)
);
CREATE TABLE IF NOT EXISTS pg_connector_validation_runs (
    validation_run_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL REFERENCES pg_mcps(id) ON DELETE CASCADE,
    payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS pg_validation_connector ON pg_connector_validation_runs(connector_id);
CREATE TABLE IF NOT EXISTS pg_connector_activations (
    activation_id TEXT PRIMARY KEY,
    mcp_id TEXT NOT NULL REFERENCES pg_mcps(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    previous_revision_id TEXT,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
