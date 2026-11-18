-- §21.4: the §4 command contract tables (faithful port of the SQLite
-- durable-execution schema) + the §21.21 feedback-adjacent command wait
-- table. Applied as a NEW migration per the ledger rule.
CREATE TABLE IF NOT EXISTS pg_commands_v2 (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    actor_type TEXT NOT NULL DEFAULT 'user',
    source TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    idempotency_scoped TEXT UNIQUE,
    request_digest TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    run_id TEXT,
    build_id TEXT,
    parent_command_id TEXT,
    parent_run_id TEXT,
    parent_task_id TEXT,
    state TEXT NOT NULL DEFAULT 'ACCEPTED',
    state_version INTEGER NOT NULL DEFAULT 1,
    waiting_reason TEXT,
    deadline_at BIGINT,
    cancel_requested_at BIGINT,
    cancel_requested_by TEXT,
    cancel_reason TEXT,
    cancel_acknowledged_at BIGINT,
    terminal_reason TEXT,
    result_reference TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pg_commands_owner_state ON pg_commands_v2(owner_user_id, state);
CREATE INDEX IF NOT EXISTS idx_pg_commands_run ON pg_commands_v2(run_id);

CREATE TABLE IF NOT EXISTS pg_outbox_v2 (
    event_id TEXT PRIMARY KEY,
    command_id TEXT,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL DEFAULT 1,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at BIGINT NOT NULL,
    available_at BIGINT NOT NULL,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    dispatcher_owner TEXT,
    dispatcher_lease_expires_at BIGINT,
    published_at BIGINT,
    last_error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_pg_outbox_v2_unpublished
    ON pg_outbox_v2(published_at, available_at);

CREATE TABLE IF NOT EXISTS pg_pending_commands (
    command_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    org_id TEXT,
    workload_class TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    idempotency_key TEXT UNIQUE,
    request_digest TEXT,
    created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pg_pending_user ON pg_pending_commands(user_id, status);
CREATE INDEX IF NOT EXISTS idx_pg_pending_expiry ON pg_pending_commands(expires_at);
