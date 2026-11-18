-- Migration 0027 — idempotency ledger on PostgreSQL (§32.6 store parity).
--
-- The execution-dedup contract (backend/repositories/base.py:
-- BaseIdempotencyRepository — atomic_insert / commit_success /
-- commit_uncertain / reconcile_uncertain / sweep_stale_running / …) was
-- implemented on SQLite and Firestore but the PostgreSQL repository only
-- exposed the legacy get_or_put cache, so the deployed durable profile
-- crashed with AttributeError the first time a tool call claimed its
-- ledger lock (observed live: workflow tasks FAILED with
-- "'PostgresIdempotencyRepository' object has no attribute
-- 'atomic_insert'").
--
-- Expand-only (§32.20.3): no column is dropped or rewritten; legacy
-- cache rows keep their responses and become terminal 'completed'
-- entries so a claim can never collide with an old cache row.

ALTER TABLE pg_idempotency
    ADD COLUMN IF NOT EXISTS workflow_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS task_id TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'running',
    ADD COLUMN IF NOT EXISTS result_payload TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_pg_idempotency_wf_task
    ON pg_idempotency (workflow_id, task_id);
CREATE INDEX IF NOT EXISTS idx_pg_idempotency_status_updated
    ON pg_idempotency (status, updated_at);

-- Legacy cache rows (written by get_or_put before the ledger columns
-- existed) are terminal responses, not live locks.
UPDATE pg_idempotency SET status = 'completed'
WHERE updated_at = created_at AND workflow_id = '' AND task_id = '';
