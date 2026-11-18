-- §21.4: parent_waits (the §4 waiter contract) on Postgres.
CREATE TABLE IF NOT EXISTS parent_waits (
    wait_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    parent_task_id TEXT,
    child_command_id TEXT,
    child_run_id TEXT,
    condition TEXT NOT NULL DEFAULT 'capability_ready',
    state TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pg_waits_parent ON parent_waits(parent_run_id);
