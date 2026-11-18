ALTER TABLE pg_memory_cleanup_jobs ADD COLUMN IF NOT EXISTS user_id TEXT;
ALTER TABLE pg_memory_cleanup_jobs ADD COLUMN IF NOT EXISTS dependents JSONB;
