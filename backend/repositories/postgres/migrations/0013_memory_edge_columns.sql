-- §21.19: unify the memory dependency edge columns with the sqlite
-- contract (derived_kind/derived_id).
ALTER TABLE pg_memory_dependencies RENAME COLUMN dependent_kind TO derived_kind;
ALTER TABLE pg_memory_dependencies RENAME COLUMN dependent_id TO derived_id;
