-- §27.13 (Phase 27): bring pg_deliverables onto the shared §17.7
-- deliverable contract (candidates, revision keys, verification
-- transitions, ownership-first lookups). The legacy dict-contract
-- repository refused the production publication path
-- (create_candidate(deliverable_key=...) → TypeError), which broke
-- generated-website, model-asset and resume publication on the
-- durable PostgreSQL profile.
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS deliverable_key TEXT;
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS owner_user_id TEXT;
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS mime_type TEXT NOT NULL DEFAULT 'text/markdown';
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS lineage JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS access_policy TEXT NOT NULL DEFAULT 'owner';
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS parent_revision INTEGER;
ALTER TABLE pg_deliverables ADD COLUMN IF NOT EXISTS verification_json JSONB NOT NULL DEFAULT '{}'::jsonb;
-- Contract rows may exist before any run is attached.
ALTER TABLE pg_deliverables ALTER COLUMN run_id DROP NOT NULL;
-- Backfill ownership from the legacy user_id column.
UPDATE pg_deliverables SET owner_user_id = user_id WHERE owner_user_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_deliverables_key_revision
  ON pg_deliverables (deliverable_key, revision) WHERE deliverable_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pg_deliverables_owner
  ON pg_deliverables (owner_user_id, updated_at);

-- Revision chain identity per deliverable_key (§17.7 stage 1).
CREATE TABLE IF NOT EXISTS pg_deliverable_keys (
  deliverable_key TEXT PRIMARY KEY,
  current_revision INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
