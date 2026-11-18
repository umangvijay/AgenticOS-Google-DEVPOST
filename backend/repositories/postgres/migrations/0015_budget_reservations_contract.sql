-- Phase 22 §22.3 (found by the live stack): the runtime budget contract
-- (repositories.PostgresBudgetRepository, full §2 state machine) writes
-- columns that 0001_core.sql's minimal budget_reservations lacks, while
-- the §20.4 simple writer (workflow_repository.PostgresBudgetRepository)
-- keeps using id/reserved/settled. This migration is ADDITIVE and makes
-- the table satisfy BOTH writers per the ledger rule (never edit an
-- applied migration): the legacy key `id` stays; the runtime key
-- `reservation_id` is added and backfilled from id so legacy rows keep
-- their identity; request_key is backfilled the same way before its
-- NOT NULL + UNIQUE contract is enforced.
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS reservation_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS request_key TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS request_digest TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS organization_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS task_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS call_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'gemini';
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS model TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS credential_owner_reference TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'generation';
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS units INTEGER;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS settled_units INTEGER;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS pricing_version TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS buckets_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS receipt_digest TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS provider_request_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS measurement_source TEXT NOT NULL DEFAULT 'estimated';
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS record_version INTEGER NOT NULL DEFAULT 0;
-- Legacy rows keep their identity under the new runtime key columns.
UPDATE budget_reservations SET reservation_id = COALESCE(reservation_id, id);
UPDATE budget_reservations SET request_key = COALESCE(request_key, id);
UPDATE budget_reservations SET units = COALESCE(units, reserved)
  WHERE units IS NULL;
UPDATE budget_reservations SET updated_at = COALESCE(updated_at, created_at);
ALTER TABLE budget_reservations ALTER COLUMN units SET NOT NULL;
ALTER TABLE budget_reservations ALTER COLUMN request_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_reservations_reservation_id
  ON budget_reservations(reservation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_reservations_request_key
  ON budget_reservations(request_key);
CREATE INDEX IF NOT EXISTS idx_budget_res_user_period
  ON budget_reservations(user_id, period);
CREATE INDEX IF NOT EXISTS idx_budget_res_state
  ON budget_reservations(state, updated_at);
