-- §21.3: the sqlite callback passes ISO strings for created_at — relax
-- the build row's clock columns to TEXT (sqlite-parity row shape).
ALTER TABLE pg_mcp_builds ALTER COLUMN created_at DROP NOT NULL;
ALTER TABLE pg_mcp_builds ALTER COLUMN updated_at DROP NOT NULL;
ALTER TABLE pg_mcp_builds ALTER COLUMN created_at TYPE TEXT USING created_at::text;
ALTER TABLE pg_mcp_builds ALTER COLUMN updated_at TYPE TEXT USING updated_at::text;
