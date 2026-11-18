-- §21.3 correction: 0001 pointed workflows/tasks/deliverables FKs at a
-- legacy `users` table while the auth layer owns pg_users; ownership is
-- enforced at the application layer (tenant scoping), so the wrong
-- constraints are dropped rather than redirected.
ALTER TABLE workflows DROP CONSTRAINT IF EXISTS workflows_user_id_fkey;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_user_id_fkey;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_run_id_fkey;
ALTER TABLE deliverables DROP CONSTRAINT IF EXISTS deliverables_user_id_fkey;
ALTER TABLE deliverables DROP CONSTRAINT IF EXISTS deliverables_run_id_fkey;
ALTER TABLE commands DROP CONSTRAINT IF EXISTS commands_run_id_fkey;
ALTER TABLE commands DROP CONSTRAINT IF EXISTS commands_user_id_fkey;
