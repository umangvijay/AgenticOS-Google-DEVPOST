-- Pre-migration revocations retain their inactive flag and unknown timestamp.
ALTER TABLE pg_api_keys ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
