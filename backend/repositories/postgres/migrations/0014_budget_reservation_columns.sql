-- §21.3: reconcile budget_reservations to the §2 contract columns the
-- repository implements (0001 shipped a minimal shape).
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS reservation_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS request_key TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS request_digest TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS call_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'tokens';
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS settled_units INTEGER NOT NULL DEFAULT 0;
ALTER TABLE budget_reservations ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ;
UPDATE budget_reservations SET reservation_id = id WHERE reservation_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_reservations_rid
    ON budget_reservations(reservation_id) WHERE reservation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_budget_reservations_request_key
    ON budget_reservations(request_key);
