-- §21.3 correction: task ids are per-run (sqlite PK is (run_id, task_id));
-- a global PK on id collides across runs. Composite key + supporting index.
ALTER TABLE tasks DROP CONSTRAINT tasks_pkey;
ALTER TABLE tasks ADD PRIMARY KEY (run_id, id);
