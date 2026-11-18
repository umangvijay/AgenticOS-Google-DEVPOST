CREATE TABLE agent_tasks (
    contract_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    role TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    dedup_key TEXT,
    -- Immutable inputs, delegation constraints and reproducibility fields.
    specification JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PROPOSED',
    state_version BIGINT NOT NULL DEFAULT 1,
    owner_worker_id TEXT,
    lease_expires_at BIGINT NOT NULL DEFAULT 0,
    deadline_at BIGINT NOT NULL,
    budget_units BIGINT NOT NULL CHECK (budget_units >= 0),
    spent_units BIGINT NOT NULL DEFAULT 0 CHECK (spent_units >= 0),
    result_json TEXT,
    error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX agent_tasks_parent ON agent_tasks(parent_id, created_at, contract_id);
CREATE INDEX agent_tasks_pending ON agent_tasks(created_at, contract_id) WHERE state='PROPOSED';
CREATE INDEX agent_tasks_expiring ON agent_tasks(deadline_at) WHERE state IN ('PROPOSED','ASSIGNED','RUNNING');
CREATE INDEX agent_tasks_reclaim ON agent_tasks(lease_expires_at) WHERE state='RUNNING';
