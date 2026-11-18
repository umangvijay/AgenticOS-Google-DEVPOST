-- §20.4 core durable subset. Explicit schema; TEXT ids (application-
-- generated, UUID-shaped), TIMESTAMPTZ clocks, JSONB payloads.

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    org_id TEXT,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workflows_user ON workflows(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    task_index INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, task_index);
-- Task lease claim (§ durable execution): only a task whose lease is
-- absent or expired may be claimed; the winner is decided atomically.
-- (Partial-index predicates must be IMMUTABLE, so now() is not
-- expressible here; the atomic lease-claim UPDATE carries the guard,
-- and this plain index serves expired-lease scans.)
CREATE INDEX IF NOT EXISTS idx_tasks_lease_expiry ON tasks(lease_expires_at);

CREATE TABLE IF NOT EXISTS commands (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    command_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'received',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
-- Event append is the ordered source of truth; BIGSERIAL sequence gives
-- replay/reconciliation a stable cursor (§20.5 pairing with the bus).

CREATE TABLE IF NOT EXISTS deliverables (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id),
    deliverable_type TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'drafted',
    revision INTEGER NOT NULL DEFAULT 1,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_checksum TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deliverables_run ON deliverables(run_id);

CREATE TABLE IF NOT EXISTS budget_reservations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT,
    scope TEXT NOT NULL DEFAULT 'user',
    unit TEXT NOT NULL DEFAULT 'tokens',
    period TEXT NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0,
    settled INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'reserved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_budget_user_period ON budget_reservations(user_id, period);
