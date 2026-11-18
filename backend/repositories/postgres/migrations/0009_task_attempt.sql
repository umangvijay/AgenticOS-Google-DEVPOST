-- §21.3: the durable attempt counter (lease/claim contract).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS defer_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
