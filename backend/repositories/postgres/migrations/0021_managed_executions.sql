CREATE TABLE IF NOT EXISTS managed_executions (
    id TEXT PRIMARY KEY,
    runner_host_id TEXT NOT NULL,
    request JSONB NOT NULL,
    state TEXT NOT NULL,
    cleanup TEXT NOT NULL,
    deadline DOUBLE PRECISION NOT NULL,
    result JSONB,
    reason TEXT,
    stopped DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS managed_executions_host_deadline
    ON managed_executions (runner_host_id, deadline)
    WHERE state IN ('queued','running','cancel_requested') OR cleanup='orphaned';
