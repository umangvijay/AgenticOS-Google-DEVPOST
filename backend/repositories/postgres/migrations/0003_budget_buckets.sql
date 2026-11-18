-- §21.3: budget bucket state (the §2.4 equation) — added after 0001
-- shipped with reservations only; a NEW migration per the ledger rule.
CREATE TABLE IF NOT EXISTS budget_buckets (
    scope TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    period TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'tokens',
    consumed INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    uncertain_reserved INTEGER NOT NULL DEFAULT 0,
    legacy_seed INTEGER NOT NULL DEFAULT 0,
    limit_applied INTEGER,
    policy_version TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, subject_id, period, unit)
);
CREATE TABLE IF NOT EXISTS budget_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    reservation_id TEXT,
    scope TEXT NOT NULL DEFAULT 'user',
    subject_id TEXT NOT NULL DEFAULT '',
    period TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL DEFAULT 'tokens',
    units INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
