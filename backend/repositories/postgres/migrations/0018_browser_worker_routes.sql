ALTER TABLE pg_browser_sessions ADD COLUMN IF NOT EXISTS owner_worker_id TEXT;
ALTER TABLE pg_browser_sessions ADD COLUMN IF NOT EXISTS runtime_session_id TEXT;
