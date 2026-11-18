-- Preserve legacy viewer-routing rows while adding the typed WebAgent contract.
ALTER TABLE pg_browser_sessions ADD COLUMN IF NOT EXISTS session_metadata JSONB;
