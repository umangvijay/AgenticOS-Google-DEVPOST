-- §21.3: the full application repository inventory on Postgres.
-- users, sessions (refresh tokens + denylist), organizations, credentials
-- (secrets + api keys), tools (mcp registry + builds), commands + approvals,
-- effects, browser ownership, recovery episodes, evaluations, memory,
-- deliverables, feedback (§21.21), notifications, settings, audit,
-- idempotency, schedules, experiments, execution steps, usage budgets.

CREATE TABLE IF NOT EXISTS pg_users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT,
    auth_provider TEXT NOT NULL DEFAULT 'local',
    google_id TEXT UNIQUE,
    avatar_url TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ,
    last_login TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_refresh_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pg_refresh_user ON pg_refresh_tokens(user_id);

CREATE TABLE IF NOT EXISTS pg_token_denylist (
    jti TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pg_org_members (
    org_id TEXT NOT NULL REFERENCES pg_orgs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);
CREATE TABLE IF NOT EXISTS pg_org_invitations (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES pg_orgs(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'pending',
    token TEXT UNIQUE NOT NULL,
    invited_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_secrets (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    ciphertext TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS pg_api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    key_hash TEXT UNIQUE NOT NULL,
    prefix TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pg_mcps (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL DEFAULT 'adapter',
    trust_tier TEXT NOT NULL DEFAULT 'candidate',
    spec_provenance TEXT NOT NULL DEFAULT '',
    source_spec TEXT,
    base_url TEXT,
    auth_type TEXT NOT NULL DEFAULT 'none',
    credential_ref TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    is_public BOOLEAN NOT NULL DEFAULT FALSE,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    state TEXT NOT NULL DEFAULT 'registered',
    tools_cache JSONB NOT NULL DEFAULT '[]'::jsonb,
    test_coverage JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pg_mcp_builds (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'spec',
    source TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    mcp_id TEXT,
    error TEXT,
    parent_run_id TEXT,
    origin TEXT NOT NULL DEFAULT 'user',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_commands (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT,
    command_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'RECEIVED',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB,
    idempotency_key TEXT UNIQUE,
    deadline_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pg_outbox_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT UNIQUE NOT NULL,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pg_outbox_pending ON pg_outbox_events(state, available_at);
CREATE TABLE IF NOT EXISTS pg_approvals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL DEFAULT 'tool_approval',
    status TEXT NOT NULL DEFAULT 'PENDING',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    decision_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pg_effects (
    effect_id TEXT PRIMARY KEY,
    operation_name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    run_id TEXT,
    task_id TEXT,
    command_id TEXT,
    target TEXT NOT NULL DEFAULT '',
    canonical_arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PREPARED',
    state_version INTEGER NOT NULL DEFAULT 1,
    external_account_id TEXT,
    credential_reference TEXT,
    approval_reference TEXT,
    outcome JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_effects_digest ON pg_effects(owner_user_id, operation_name, request_digest)
    WHERE state <> 'CANCELLED';
CREATE TABLE IF NOT EXISTS pg_effect_receipts (
    id BIGSERIAL PRIMARY KEY,
    effect_id TEXT NOT NULL REFERENCES pg_effects(effect_id),
    attempt_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_digest TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_browser_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE',
    controller_worker_id TEXT,
    controller_epoch TEXT,
    lease_expires_at TIMESTAMPTZ,
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    challenge JSONB,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pg_browser_checkpoints (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES pg_browser_sessions(session_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_recovery_episodes (
    episode_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT 'system',
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    failure_code TEXT NOT NULL,
    failure_category TEXT NOT NULL,
    goal_digest TEXT NOT NULL DEFAULT '',
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    state TEXT NOT NULL DEFAULT 'DETECTED',
    state_version INTEGER NOT NULL DEFAULT 1,
    owner_worker_id TEXT,
    owner_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    verification_calls INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_recovery_active ON pg_recovery_episodes(tenant_id, run_id, task_id)
    WHERE state NOT IN ('VERIFIED_RECOVERY','RECOVERY_FAILED','CANCELLED','SUPERSEDED','ESCALATED');

CREATE TABLE IF NOT EXISTS pg_evaluations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    identity_digest TEXT NOT NULL,
    outcome TEXT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_class TEXT NOT NULL,
    embedding JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source TEXT,
    scope TEXT NOT NULL DEFAULT 'personal',
    scope_ref TEXT,
    subject_key TEXT,
    verification_status TEXT NOT NULL DEFAULT 'inferred',
    source_event_id TEXT,
    observed_at BIGINT,
    expires_at BIGINT,
    superseded_by TEXT,
    deletion_state TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pg_memories_user ON pg_memories(user_id);
CREATE TABLE IF NOT EXISTS pg_memory_dependencies (
    id BIGSERIAL PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES pg_memories(id) ON DELETE CASCADE,
    dependent_kind TEXT NOT NULL,
    dependent_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (memory_id, dependent_kind, dependent_id)
);
CREATE TABLE IF NOT EXISTS pg_memory_cleanup_jobs (
    id BIGSERIAL PRIMARY KEY,
    memory_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_deliverables (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    deliverable_type TEXT NOT NULL,
    lifecycle_state TEXT NOT NULL DEFAULT 'drafted',
    revision INTEGER NOT NULL DEFAULT 1,
    parent_revision INTEGER,
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    file_manifest JSONB NOT NULL DEFAULT '[]'::jsonb,
    content_checksum TEXT NOT NULL DEFAULT '',
    verification JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pg_publication_jobs (
    id TEXT PRIMARY KEY,
    deliverable_id TEXT NOT NULL REFERENCES pg_deliverables(id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'render',
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    completion_emitted BOOLEAN NOT NULL DEFAULT FALSE,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    helpful BOOLEAN NOT NULL,
    category TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    expected_result TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    task_id TEXT,
    tool_id TEXT,
    deliverable_revision TEXT,
    model_version TEXT,
    policy_version TEXT,
    app_version TEXT NOT NULL DEFAULT '',
    share_diagnostics BOOLEAN NOT NULL DEFAULT FALSE,
    triage_status TEXT NOT NULL DEFAULT 'received',
    triage_actor TEXT,
    triage_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pg_feedback_user ON pg_feedback(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pg_feedback_triage ON pg_feedback(triage_status, created_at);

CREATE TABLE IF NOT EXISTS pg_notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    read BOOLEAN NOT NULL DEFAULT FALSE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_user_settings (
    user_id TEXT PRIMARY KEY,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_audit_events (
    id BIGSERIAL PRIMARY KEY,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_idempotency (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT '',
    response JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_schedules (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    cron TEXT NOT NULL,
    goal TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_experiments (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'PROPOSED',
    hypothesis TEXT NOT NULL DEFAULT '',
    expected_benefit TEXT NOT NULL DEFAULT '',
    baseline_ref TEXT NOT NULL DEFAULT '',
    candidate_ref TEXT NOT NULL DEFAULT '',
    dataset_ref TEXT NOT NULL DEFAULT '',
    dataset_hash TEXT NOT NULL DEFAULT '',
    grading_hash TEXT NOT NULL DEFAULT '',
    thresholds JSONB NOT NULL DEFAULT '{}'::jsonb,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    results JSONB,
    proposed_by TEXT NOT NULL DEFAULT '',
    approved_by TEXT,
    rolled_back_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_execution_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'OBSERVING',
    state_version INTEGER NOT NULL DEFAULT 1,
    action TEXT NOT NULL DEFAULT '',
    observation JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_usage_daily (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);


CREATE TABLE IF NOT EXISTS pg_plugins (
    id TEXT PRIMARY KEY,
    manifest JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'INSTALLED',
    installed_by TEXT NOT NULL DEFAULT '',
    installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pg_content (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'document',
    content JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE IF NOT EXISTS pg_adaptive_policy (
    context_key TEXT NOT NULL DEFAULT 'global',
    model TEXT NOT NULL,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    total_latency_ms NUMERIC NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (context_key, model)
);

CREATE TABLE IF NOT EXISTS pg_schedule_full (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    cron TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
