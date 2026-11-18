"""
AgentOS — SQLite Database Manager

Async SQLite via aiosqlite. WAL mode for concurrent reads.
Schema migration on startup (create tables if not exist).
Database file at data/agentos.db, auto-created.
"""

import asyncio
import os
import logging
from contextlib import asynccontextmanager
import aiosqlite
from pathlib import Path
from typing import AsyncIterator, Optional

from backend.config.settings import settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  SCHEMA — All tables per spec
# ═══════════════════════════════════════════════════════════════════

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    helpful INTEGER NOT NULL, category TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '', expected_result TEXT NOT NULL DEFAULT '',
    run_id TEXT, task_id TEXT, tool_id TEXT, deliverable_revision TEXT,
    model_version TEXT, policy_version TEXT, app_version TEXT NOT NULL DEFAULT '',
    share_diagnostics INTEGER NOT NULL DEFAULT 0,
    triage_status TEXT NOT NULL DEFAULT 'received', triage_actor TEXT, triage_note TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_user_created ON feedback(user_id, created_at);
-- ── Users ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT,                  -- NULL for Google OAuth users
    auth_provider TEXT NOT NULL DEFAULT 'local',  -- 'local' | 'google'
    google_id TEXT UNIQUE,               -- Google OAuth sub claim
    avatar_url TEXT,
    role TEXT NOT NULL DEFAULT 'user',    -- 'admin' | 'user' | 'viewer'
    is_active INTEGER NOT NULL DEFAULT 1,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,                   -- ISO timestamp
    last_login TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);

-- ── User Settings ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    settings_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

-- ── Refresh Tokens ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_expires ON refresh_tokens(expires_at);

-- ── Token Denylist (server-side logout) ──────────────────────────
CREATE TABLE IF NOT EXISTS token_denylist (
    jti TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_denylist_expires ON token_denylist(expires_at);

-- ── Per-user API keys (programmatic access) ──────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- ── Orgs / teams (enterprise RBAC foundation) ────────────────────
CREATE TABLE IF NOT EXISTS orgs (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orgs_owner ON orgs(owner_id);

CREATE TABLE IF NOT EXISTS org_members (
    org_id TEXT NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member',
    added_at TEXT NOT NULL,
    added_by TEXT,
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id);

-- ── Adaptive model policy (online learning from task outcomes) ───
CREATE TABLE IF NOT EXISTS model_stats (
    model TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    ewma_latency_ms REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- ── Workflows ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL DEFAULT 'default_workflow',
    user_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_user ON workflow_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status);

-- ── Tasks ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    input_data TEXT NOT NULL DEFAULT '{}',   -- JSON
    output_data TEXT,                         -- JSON
    dependencies TEXT NOT NULL DEFAULT '[]',  -- JSON array
    started_at TEXT,
    completed_at TEXT,
    lease_started_at TEXT,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    timeout_seconds INTEGER NOT NULL DEFAULT 60,
    max_retries INTEGER NOT NULL DEFAULT 3,
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    requested_outcomes_json TEXT NOT NULL DEFAULT '[]',
    allowed_action_scope_json TEXT NOT NULL DEFAULT '[]',
    completion_policy TEXT NOT NULL DEFAULT 'criteria',
    -- Phase 11 §11.0: same columns are used by the existing migration
    recovery_enabled INTEGER NOT NULL DEFAULT 0,
    max_recoveries INTEGER NOT NULL DEFAULT 3,
    max_total_attempts INTEGER NOT NULL DEFAULT 5,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    original_input TEXT,                      -- JSON
    recovery_history TEXT DEFAULT '[]',       -- JSON array
    error TEXT,
    error_type TEXT,
    trace_id TEXT,
    PRIMARY KEY (run_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ── Events (SSE Timeline) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT,
    status TEXT,
    summary TEXT NOT NULL,
    sanitized_metadata TEXT NOT NULL DEFAULT '{}',  -- JSON
    sequence INTEGER                                 -- Auto-increment for ordering
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

-- ── Approvals ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    risk_level INTEGER NOT NULL,
    autonomy_level INTEGER NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',    -- JSON
    arguments_hash TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT 'system',
    status TEXT NOT NULL DEFAULT 'PENDING',
    decision_by TEXT,
    decision_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_user_status ON approvals(user_id, status);

-- ── MCP Integrations ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mcps (
    mcp_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    transport TEXT NOT NULL,
    auth_json TEXT NOT NULL DEFAULT '{}',    -- JSON AuthMetadata
    scopes TEXT NOT NULL DEFAULT '[]',       -- JSON array
    health TEXT NOT NULL DEFAULT 'UNKNOWN',
    health_updated_at TEXT,
    state TEXT NOT NULL DEFAULT 'DRAFT',     -- ConnectorState
    spec_hash TEXT,
    spec_version TEXT,
    source_uri TEXT,
    built_at TEXT,
    owner TEXT NOT NULL DEFAULT 'system',
    is_enabled INTEGER NOT NULL DEFAULT 1,
    active_revision_id TEXT,                 -- Phase 6 §6.14: active pointer
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcps_owner ON mcps(owner);

-- ── Phase 6 §6.14: revision activation history ─────────────────
CREATE TABLE IF NOT EXISTS connector_activations (
    activation_id TEXT PRIMARY KEY,
    mcp_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    previous_revision_id TEXT,
    actor TEXT NOT NULL DEFAULT 'system',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activations_mcp ON connector_activations(mcp_id, created_at);

-- ── Phase 12: durable evaluation records ───────────────────────
-- Evaluation records survive restart and reproduce results for fixed
-- fixtures. Scorer changes cannot rewrite historical scores silently.
CREATE TABLE IF NOT EXISTS evaluation_records (
    evaluation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    workflow_id TEXT,
    agent_name TEXT,
    model TEXT,
    task_type TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    successful_recoveries INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    unknown_tool_call_rate REAL,
    invalid_argument_rate REAL,
    tool_schema_violation_rate REAL,
    latency_ms REAL,
    -- §12.9: NULL means the measurement is UNAVAILABLE — never confused
    -- with zero usage or perfect quality.
    token_usage_total INTEGER,
    token_usage_input INTEGER,
    token_usage_output INTEGER,
    evaluator_version TEXT NOT NULL DEFAULT '1.0.0',
    prompt_version TEXT NOT NULL DEFAULT '1.0.0',
    fixture_hash TEXT,
    effect_states_json TEXT NOT NULL DEFAULT '{}',
    criterion_results_json TEXT NOT NULL DEFAULT '[]',
    evidence_references_json TEXT NOT NULL DEFAULT '[]',
    confidence_limitations_json TEXT NOT NULL DEFAULT '[]',
    -- §12.11: an intentional re-evaluation links to the previous result.
    previous_evaluation_id TEXT,
    created_at_epoch INTEGER NOT NULL,
    record_json TEXT NOT NULL DEFAULT '{}'
);
-- §12.11: duplicate delivery returns the ORIGINAL — one record per
-- (run, scorer version).
CREATE UNIQUE INDEX IF NOT EXISTS idx_evaluations_identity
ON evaluation_records(run_id, evaluator_version);
CREATE INDEX IF NOT EXISTS idx_evaluations_run ON evaluation_records(run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_created ON evaluation_records(created_at_epoch);

-- ── Phase 13: controlled self-improvement experiments ──────────
-- Candidate changes with immutable inputs (baseline/candidate refs,
-- pinned dataset + grading hashes), predeclared thresholds, separated
-- permission roles, and promotion/rollback evidence.
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    hypothesis TEXT NOT NULL DEFAULT '',
    expected_benefit TEXT NOT NULL DEFAULT '',
    baseline_ref TEXT NOT NULL DEFAULT '',
    candidate_ref TEXT NOT NULL DEFAULT '',
    candidate_diff TEXT NOT NULL DEFAULT '',
    dataset_ref TEXT NOT NULL DEFAULT '',
    dataset_hash TEXT NOT NULL DEFAULT '',
    grading_hash TEXT NOT NULL DEFAULT '',
    thresholds_json TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'PROPOSED',
    state_version INTEGER NOT NULL DEFAULT 1,
    results_json TEXT NOT NULL DEFAULT '{}',
    proposed_by TEXT NOT NULL DEFAULT '',
    evaluated_by TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    activated_by TEXT NOT NULL DEFAULT '',
    rolled_back_by TEXT NOT NULL DEFAULT '',
    cohort_size INTEGER,
    promoted_dataset_hash TEXT,
    promoted_results_json TEXT,
    promoted_actor TEXT,
    deployment_version TEXT,
    rollback_reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiments_state ON experiments(state);
-- §13.17: durable canary exposure — the absolute cohort cap is enforced
-- against COUNTED admissions, not a hash fraction over an unknown
-- population. One row per (experiment, subject); re-exposure is idempotent.
CREATE TABLE IF NOT EXISTS experiment_exposures (
    experiment_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    variant TEXT NOT NULL DEFAULT 'candidate',
    assigned_at INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_exposures_experiment ON experiment_exposures(experiment_id, variant);
CREATE INDEX IF NOT EXISTS idx_experiments_created ON experiments(created_at);

-- ── Phase 13: versioned prompt store ───────────────────────────
-- Immutable prompt versions with an activation pointer per prompt id.
-- Rollback = moving the pointer back (rows are never rewritten).
CREATE TABLE IF NOT EXISTS prompt_versions (
    version_id TEXT PRIMARY KEY,
    prompt_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    template TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(prompt_id, version)
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_prompt
ON prompt_versions(prompt_id, version DESC);

CREATE TABLE IF NOT EXISTS prompt_activations (
    prompt_id TEXT PRIMARY KEY,
    active_version_id TEXT NOT NULL,
    activated_by TEXT NOT NULL DEFAULT '',
    activated_at INTEGER NOT NULL
);

-- ── Phase 13: versioned routing-policy snapshots ───────────────
-- Durable snapshots of the adaptive policy's stats; rollback restores
-- the counters (the learner never authorizes anything itself).
CREATE TABLE IF NOT EXISTS policy_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    stats_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- ── Phase 15: agent task contracts (bounded delegation) ────────
-- Every delegated task: parent, typed inputs/outputs, allowed tools,
-- budget, deadline, lease, cancellation. §15.5 bounds (depth, fan-out,
-- concurrent children, spend) are enforced in transactions.
CREATE TABLE IF NOT EXISTS agent_tasks (
    contract_id TEXT PRIMARY KEY,
    parent_kind TEXT NOT NULL DEFAULT 'run',
    parent_id TEXT NOT NULL,
    ancestor_ids_json TEXT NOT NULL DEFAULT '[]',
    role TEXT NOT NULL DEFAULT 'outcome_verifier',
    tenant_id TEXT NOT NULL DEFAULT '',
    dedup_key TEXT,
    input_data_json TEXT NOT NULL DEFAULT '{}',
    output_schema_json TEXT NOT NULL DEFAULT '{}',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    budget_units INTEGER NOT NULL DEFAULT 0,
    deadline_at INTEGER NOT NULL DEFAULT 0,
    depth INTEGER NOT NULL DEFAULT 0,
    completion_policy TEXT NOT NULL DEFAULT 'require_all',
    -- §15.3: the complete delegation contract.
    goal TEXT NOT NULL DEFAULT '',
    exclusions_json TEXT NOT NULL DEFAULT '[]',
    input_refs_json TEXT NOT NULL DEFAULT '[]',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    retry_policy TEXT NOT NULL DEFAULT 'bounded',
    agent_version TEXT NOT NULL DEFAULT '',
    tool_version TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'PROPOSED',
    state_version INTEGER NOT NULL DEFAULT 1,
    owner_worker_id TEXT,
    lease_expires_at INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT,
    spent_units INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent ON agent_tasks(parent_id, state);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_tenant ON agent_tasks(tenant_id, created_at);
-- §15 acceptance: duplicate assignment dedup — one ACTIVE contract per
-- (parent, dedup_key); a terminal contract frees the pair.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tasks_dedup
ON agent_tasks(parent_id, dedup_key)
WHERE dedup_key IS NOT NULL AND
      state NOT IN ('COMPLETED','FAILED','TIMED_OUT','CANCELLED','SUPERSEDED');

-- ── Phase 14 §14.0: the evidence register ──────────────────────
-- Every phase subphase's acceptance claim is a QUERYABLE row bound to
-- the test that proves it — a claim without a test reference is
-- refused, so the register cannot hold unevidenced "complete" rows.
CREATE TABLE IF NOT EXISTS evidence_register (
    subphase_id TEXT PRIMARY KEY,
    claim TEXT NOT NULL,
    status TEXT NOT NULL,
    test_ref TEXT,
    evidence_level TEXT NOT NULL DEFAULT 'verified_locally',
    recorded_by TEXT NOT NULL DEFAULT '',
    recorded_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- ── Phase 14 §14.J: durable activation receipts ────────────────
-- The promotion coordinator's activation receipt, persisted at the
-- moment activation is ATTEMPTED (state pending) and marked after —
-- a crash between promotion and activation is reconcilable by identity.
CREATE TABLE IF NOT EXISTS activation_receipts (
    receipt_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL UNIQUE,
    prompt_id TEXT NOT NULL,
    candidate_version_id TEXT NOT NULL,
    state TEXT NOT NULL,
    active_version_id TEXT,
    error TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- ── Phase 14 §14.18: durable reward events ─────────────────────
-- Immutable reward events with identity based on (decision_id,
-- evaluation_id, reward_version). Each learner consumes through a
-- transaction that applies the statistics update and records
-- consumption atomically — crashes before/during/after application
-- neither lose nor duplicate learning.
CREATE TABLE IF NOT EXISTS reward_events (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    evaluation_id TEXT NOT NULL,
    reward_version TEXT NOT NULL DEFAULT 'v1',
    reward_class TEXT NOT NULL,
    reward_value REAL NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    context_key TEXT NOT NULL DEFAULT '',
    latency_ms REAL,
    cost_units REAL,
    created_at INTEGER NOT NULL,
    consumed_by_contextual INTEGER NOT NULL DEFAULT 0,
    consumed_by_global INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT,
    UNIQUE(decision_id, evaluation_id, reward_version)
);
CREATE INDEX IF NOT EXISTS idx_reward_events_unconsumed
ON reward_events(reward_version, consumed_by_contextual, consumed_by_global);

-- ── Phase 14: contextual bandit stats ──────────────────────────
-- Per CONTEXT-BUCKET × MODEL reward stats (verified outcomes only —
-- verified=1.0, partial=0.5, everything else 0.0 reward contribution).
CREATE TABLE IF NOT EXISTS contextual_model_stats (
    context_key TEXT NOT NULL,
    -- §14.20: EWMA cost (USD-equivalent per attempt) — a scoring
    -- tie-breaker next to latency, never part of the verified reward.
    ewma_cost_units REAL,
    model TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    reward_sum REAL NOT NULL DEFAULT 0,
    ewma_latency_ms REAL,
    recent_reward_sum REAL NOT NULL DEFAULT 0,
    recent_attempts INTEGER NOT NULL DEFAULT 0,
    recent_window_started_at INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (context_key, model)
);
-- §14.5: the exploration budget — durable windowed event log.
CREATE TABLE IF NOT EXISTS exploration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exploration_created ON exploration_events(created_at);

-- ── Phase 13 §13.11: versioned evaluation datasets ─────────────
-- Dev / held-out / adversarial sets; editing creates a new version;
-- held-out expectations are draft-proof (operator-only).
CREATE TABLE IF NOT EXISTS evaluation_datasets (
    dataset_id TEXT NOT NULL,
    name TEXT NOT NULL,
    set_kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    cases_json TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, version)
);

-- ── Phase 13 §13.16: measured shadow observations ──────────────
-- 'Shadow observed' requires RECORDED candidate execution — outcome,
-- latency, and cost — never a state label. Baseline serves the user.
CREATE TABLE IF NOT EXISTS shadow_observations (
    observation_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    candidate_version_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    latency_ms REAL,
    token_units INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_experiment ON shadow_observations(experiment_id, recorded_at);

-- ── Phase 13 §13.13: durable experiment comparisons ────────────
-- Pinned baseline/candidate cases with RESUMABLE per-case records; a
-- crash halfway resumes without rewriting completed evidence.
CREATE TABLE IF NOT EXISTS experiment_comparisons (
    comparison_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    state_version INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    baseline_ref TEXT NOT NULL DEFAULT '',
    candidate_ref TEXT NOT NULL DEFAULT '',
    cases_json TEXT NOT NULL DEFAULT '[]',
    case_results_json TEXT NOT NULL DEFAULT '[]',
    results_json TEXT,
    owner_worker_id TEXT,
    lease_expires_at INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comparisons_identity
ON experiment_comparisons(experiment_id, revision_hash);
CREATE INDEX IF NOT EXISTS idx_comparisons_state ON experiment_comparisons(state, created_at);

-- ── Phase 12 §12.11: durable evaluation jobs ───────────────────
-- Terminal events enqueue evaluation work durably; duplicate terminal
-- events return the SAME job (create-or-get); exhausted jobs stay
-- visible, never silently dropped.
CREATE TABLE IF NOT EXISTS evaluation_jobs (
    job_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    evaluator_version TEXT NOT NULL DEFAULT '3.0.0',
    reason TEXT NOT NULL DEFAULT 'run_terminal',
    state TEXT NOT NULL DEFAULT 'pending',
    state_version INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    owner_worker_id TEXT,
    lease_expires_at INTEGER,
    evaluation_id TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_jobs_identity
ON evaluation_jobs(run_id, evaluator_version);
CREATE INDEX IF NOT EXISTS idx_eval_jobs_state ON evaluation_jobs(state, created_at);

-- ── Phase 21: workflow templates (versioned) ───────────────────
-- Immutable versions; editing creates a new version (runs reference the
-- exact version they used); one active pointer per template.
CREATE TABLE IF NOT EXISTS workflow_templates (
    template_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    definition_json TEXT NOT NULL DEFAULT '{}',
    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
    config_schema_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (template_id, version)
);
CREATE INDEX IF NOT EXISTS idx_templates_tenant ON workflow_templates(tenant_id, created_at);

-- ── Phase 14: routing decision log (contextual bandit data) ────
-- Append-only; delayed verified feedback joins exactly once; missing
-- feedback stays explicit (NULL feedback_at), never zero.
CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT,
    task_category TEXT NOT NULL DEFAULT 'workflow',
    capability TEXT NOT NULL DEFAULT '',
    input_size_bytes INTEGER NOT NULL DEFAULT 0,
    latency_target_ms INTEGER,
    budget_units INTEGER,
    eligible_actions_json TEXT NOT NULL DEFAULT '[]',
    selected_action TEXT NOT NULL DEFAULT '',
    selection_mechanism TEXT NOT NULL DEFAULT 'ranked_first_fit',
    selection_probability REAL NOT NULL DEFAULT 1.0,
    latency_ms REAL,
    policy_applied INTEGER NOT NULL DEFAULT 0,
    policy_version TEXT NOT NULL DEFAULT 'ucb1-v1',
    resolved_action TEXT,
    fallback_from TEXT,
    reward_class TEXT NOT NULL DEFAULT 'missing',
    reward_verified_success INTEGER,
    feedback_at INTEGER,
    evaluation_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routing_run ON routing_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_routing_feedback ON routing_decisions(feedback_at);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing_decisions(created_at);

-- ── Phase 11 §11.2: durable recovery episodes ──────────────────
-- One recovery episode per failed task with the full §11.2 field set.
-- Ownership is atomically claimed (CAS on lease + generation).
CREATE TABLE IF NOT EXISTS recovery_episodes (
    episode_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor_id TEXT,
    command_id TEXT,
    run_id TEXT,
    task_id TEXT NOT NULL,
    failed_step_id TEXT,
    goal_digest TEXT,
    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
    allowed_scope_json TEXT NOT NULL DEFAULT '[]',
    failure_code TEXT,
    failure_category TEXT,
    provider_status TEXT,
    evidence_references_json TEXT NOT NULL DEFAULT '[]',
    proposal_action TEXT,
    proposal_arguments_json TEXT,
    proposal_digest TEXT,
    proposal_explanation TEXT,
    policy_revision TEXT,
    policy_result TEXT,
    policy_reason TEXT,
    owner_worker_id TEXT,
    owner_epoch TEXT,
    owner_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at REAL,
    state TEXT NOT NULL DEFAULT 'DETECTED',
    state_version INTEGER NOT NULL DEFAULT 1,
    deadline_at INTEGER,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    verification_calls INTEGER NOT NULL DEFAULT 0,
    budget_reservation_id TEXT,
    logical_action_id TEXT,
    effect_id TEXT,
    connector_revision_id TEXT,
    credential_binding_id TEXT,
    verification_verdict TEXT,
    verification_evidence_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    incident_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recovery_ep_task ON recovery_episodes(run_id, task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_recovery_ep_state ON recovery_episodes(state);
-- §12.5: atomic creation/deduplication — competing workers cannot create
-- duplicate ACTIVE episodes for the same failure; a terminal episode
-- frees the pair for a NEW failure round.
CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_ep_active
ON recovery_episodes(run_id, task_id)
WHERE state NOT IN ('VERIFIED_RECOVERY','RECOVERY_FAILED','ESCALATED','CANCELLED','SUPERSEDED');

-- ── Phase 10 §10.5: durable execution-step records ─────────────
-- One record per ReAct step with the §10.5 field set. The §10.6 state
-- machine uses atomic CAS transitions (expected state + version).
CREATE TABLE IF NOT EXISTS execution_steps (
    step_id TEXT PRIMARY KEY,
    logical_action_id TEXT NOT NULL,
    attempt_id TEXT,
    command_id TEXT,
    run_id TEXT,
    task_id TEXT,
    tenant_id TEXT NOT NULL,
    actor_id TEXT,
    sequence INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'OBSERVING',
    state_version INTEGER NOT NULL DEFAULT 1,
    lease_generation INTEGER,
    plan_revision TEXT,
    policy_revision TEXT,
    connector_revision TEXT,
    observation_references_json TEXT NOT NULL DEFAULT '[]',
    proposed_action TEXT,
    validated_action_digest TEXT,
    authorization_reference TEXT,
    budget_reservation_id TEXT,
    effect_id TEXT,
    result_reference TEXT,
    verification_reference TEXT,
    error_code TEXT,
    evidence_references_json TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deadline_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_exec_steps_task ON execution_steps(run_id, task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_exec_steps_logical ON execution_steps(logical_action_id);

-- ── Phase 8 §8.5: durable browser session metadata ─────────────
-- Durable METADATA only — Playwright objects stay in the process-local
-- registry keyed by (session_id, owner_epoch). Lifecycle, controller,
-- challenge, and cleanup are SEPARATE dimensions (§8.6).
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    workflow_id TEXT,
    run_id TEXT,
    task_id TEXT,
    owner_worker_id TEXT NOT NULL DEFAULT '',
    owner_epoch TEXT NOT NULL DEFAULT '',
    owner_generation INTEGER NOT NULL DEFAULT 0,
    lease_expires_at INTEGER NOT NULL DEFAULT 0,
    runtime_session_id TEXT,
    policy_revision TEXT,
    resource_state TEXT NOT NULL DEFAULT 'ALLOCATING',
    controller_state TEXT NOT NULL DEFAULT 'NONE',
    controller_generation INTEGER NOT NULL DEFAULT 0,
    challenge_state TEXT NOT NULL DEFAULT 'NONE',
    cleanup_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    allowed_origins_json TEXT NOT NULL DEFAULT '[]',
    network_policy_id TEXT,
    credential_binding_id TEXT,
    checkpoint_reference TEXT,
    last_action_id TEXT,
    created_at INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL,
    idle_expires_at INTEGER NOT NULL DEFAULT 0,
    handoff_expires_at INTEGER NOT NULL DEFAULT 0,
    absolute_expires_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_run ON browser_sessions(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_expiry ON browser_sessions(resource_state, idle_expires_at);

-- ── Phase 7 §7.11: durable runner execution records ────────────
-- Execution state is SEPARATE from cleanup state: a failed process can
-- still have resources awaiting cleanup. A crashed worker cannot leave an
-- indefinitely running sandbox — the reconciler reclaims leases past
-- expiry and marks orphans for cleanup.
CREATE TABLE IF NOT EXISTS runner_executions (
    execution_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    runtime_image_digest TEXT,
    policy_profile_id TEXT NOT NULL,
    operation_allowlist_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'queued',
    cleanup_state TEXT NOT NULL DEFAULT 'pending',  -- pending|cleaned|orphaned
    runner_assignment TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    attempt INTEGER NOT NULL DEFAULT 1,
    sandbox_identity TEXT,
    started_at REAL,
    stopped_at REAL,
    exit_reason TEXT,
    output_references_json TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runner_exec_tenant ON runner_executions(tenant_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_runner_exec_state ON runner_executions(state);

-- ── Phase 6 §6.8: durable validation runs ──────────────────────
CREATE TABLE IF NOT EXISTS connector_validation_runs (
    validation_run_id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    revision_id TEXT,
    operation_id TEXT NOT NULL,
    operation_digest TEXT NOT NULL,
    credential_binding_id TEXT NOT NULL,
    authorization_epoch TEXT NOT NULL DEFAULT 'e0',
    provider_account_id TEXT,
    environment TEXT NOT NULL DEFAULT 'production',
    runtime_digest TEXT,
    validation_profile_version TEXT NOT NULL DEFAULT 'p6.read.v1',
    case_results_json TEXT NOT NULL DEFAULT '[]',
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    valid_until INTEGER,
    artifact_references_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'running',
    superseded_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_validation_runs_connector
ON connector_validation_runs(connector_id, started_at);

CREATE TABLE IF NOT EXISTS mcp_revisions (
    mcp_id TEXT NOT NULL REFERENCES mcps(mcp_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (mcp_id, revision_id)
);

CREATE TABLE IF NOT EXISTS mcp_builds (
    build_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    logs_json TEXT NOT NULL DEFAULT '[]',
    mcp_id TEXT,
    tools_json TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    origin TEXT,
    parent_run_id TEXT,
    parent_workflow_id TEXT,
    parent_task_id TEXT,
    wait_reason TEXT,
    organization_id TEXT,
    credential_reference TEXT,             -- binding REFERENCE, never a raw secret
    required_operations_json TEXT NOT NULL DEFAULT '[]',
    intake_json TEXT NOT NULL DEFAULT '{}',  -- §29.14 application-intake record
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mcp_builds_user ON mcp_builds(user_id, created_at);

-- ── Organization invitations (audit F01) ─────────────────────────
-- Membership requires ACCEPTANCE. The token is stored hashed; the raw
-- token is returned exactly once to the inviter.
CREATE TABLE IF NOT EXISTS org_invitations (
    invitation_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    invited_by TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_invitations_org ON org_invitations(org_id, status);
CREATE INDEX IF NOT EXISTS idx_org_invitations_token ON org_invitations(token_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_org_invitations_pending_unique
ON org_invitations(org_id, email) WHERE status = 'pending';

-- ── Shared rate limiting (v5.6 §1; Phase 3 §3.4) ─────────────────
-- One fixed-window counter per (identifier, group, interval, window)
-- across all replicas sharing this database. window_start/window_end/
-- cleanup_after are EPOCH SECONDS: cleanup deletes only rows whose
-- cleanup_after (window_end + one retention window) has passed, so an
-- active counter is never swept mid-window. window_seconds is part of the
-- key so different interval configurations cannot collide.
CREATE TABLE IF NOT EXISTS rate_limits (
    identifier TEXT NOT NULL,
    group_name TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    cleanup_after INTEGER NOT NULL,
    policy_version TEXT NOT NULL DEFAULT 'p3.v1',
    PRIMARY KEY (identifier, group_name, window_seconds, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_cleanup ON rate_limits(cleanup_after);

-- ── Capacity leases (Phase 3 §3.11) ─────────────────────────────
-- Durable count of ACTIVE work per (resource_class, tenant scope).
-- `generation` is the fencing token: each acquisition for an execution
-- key increments it, so a stale worker's renew/release cannot touch a
-- newer execution's capacity. TTL expiry (acquire-time sweep +
-- reconcile_expired) reclaims units from crashed workers.
CREATE TABLE IF NOT EXISTS capacity_leases (
    lease_id TEXT PRIMARY KEY,
    resource_class TEXT NOT NULL,
    scope_key TEXT NOT NULL,               -- tenant the cap applies to
    owner_id TEXT NOT NULL,                -- authenticated principal
    execution_key TEXT NOT NULL,           -- idempotency: one lease per execution
    worker_id TEXT NOT NULL DEFAULT '',
    units INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_heartbeat INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',  -- active|released|expired|orphaned
    organization_id TEXT,
    command_id TEXT,
    task_id TEXT,
    attempt_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_active_execution
ON capacity_leases(resource_class, scope_key, execution_key) WHERE state = 'active';
CREATE INDEX IF NOT EXISTS idx_capacity_scope_active
ON capacity_leases(resource_class, scope_key, state, expires_at);

-- ── Durable command queue admission (Phase 3 §3.12) ─────────────
-- ONE accepted-command record per logical command (idempotent by
-- command_id): per-user/org/class pending caps and max-wait expiry are
-- checked against THIS table, transactionally, not against an
-- approximation of Pub/Sub backlog metrics.
CREATE TABLE IF NOT EXISTS pending_commands (
    command_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    org_id TEXT,
    workload_class TEXT NOT NULL,          -- workflow|build|webhook|schedule
    status TEXT NOT NULL DEFAULT 'pending',-- pending|dispatched|settled|expired|rejected
    idempotency_key TEXT UNIQUE,           -- STORED TENANT-SCOPED (§4.0D R8): "user|class|key"
    request_digest TEXT,                   -- payload digest: same key + different payload = conflict
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_commands(user_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_org ON pending_commands(org_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_class ON pending_commands(workload_class, status);
CREATE INDEX IF NOT EXISTS idx_pending_expiry ON pending_commands(expires_at);

-- ── Commands + outbox (Phase 4 §4.5/§4.6) ───────────────────────
-- `commands` is the durable identity + lifecycle of one accepted
-- instruction; `outbox_events` is publication intent committed IN THE SAME
-- transaction. Acceptance is atomic: command + idempotency + queue
-- accounting + initial work + outbox commit together, then the network
-- publish happens afterward and is crash-recoverable.
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    command_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    actor_type TEXT NOT NULL DEFAULT 'user',
    source TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    idempotency_scoped TEXT UNIQUE,        -- tenant|type|key (§4.4)
    request_digest TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    run_id TEXT,
    build_id TEXT,
    parent_command_id TEXT,
    parent_run_id TEXT,
    parent_task_id TEXT,
    state TEXT NOT NULL DEFAULT 'ACCEPTED',
    state_version INTEGER NOT NULL DEFAULT 1,
    waiting_reason TEXT,
    deadline_at INTEGER,
    cancel_requested_at INTEGER,
    cancel_requested_by TEXT,
    cancel_reason TEXT,
    cancel_acknowledged_at INTEGER,
    terminal_reason TEXT,
    result_reference TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_owner_state ON commands(owner_user_id, state);
CREATE INDEX IF NOT EXISTS idx_commands_run ON commands(run_id);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    command_id TEXT,
    aggregate_id TEXT NOT NULL,
    aggregate_version INTEGER NOT NULL DEFAULT 1,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    dispatcher_owner TEXT,
    dispatcher_lease_expires_at INTEGER,
    published_at INTEGER,
    last_error_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unpublished ON outbox_events(published_at, available_at);

CREATE TABLE IF NOT EXISTS parent_waits (
    wait_id TEXT PRIMARY KEY,
    parent_command_id TEXT,
    parent_run_id TEXT NOT NULL,
    parent_task_id TEXT NOT NULL,
    child_command_id TEXT,
    child_run_id TEXT,
    condition TEXT NOT NULL DEFAULT 'child_build_succeeded',
    state TEXT NOT NULL DEFAULT 'waiting',  -- waiting|resolved|failed|cancelled
    resolved_by_event_id TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_parent_waits_open
ON parent_waits(parent_run_id, parent_task_id) WHERE state = 'waiting';

-- ── Effect ledger (Phase 5 §5.4) ────────────────────────────────
-- One intended EXTERNAL action per row. Outcomes move forward only under
-- evidence; uncertainty is never rewritten by a guess (§5.12).
CREATE TABLE IF NOT EXISTS effects (
    effect_id TEXT PRIMARY KEY,
    operation_name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    organization_id TEXT,
    command_id TEXT,
    run_id TEXT,
    task_id TEXT,
    operation_instance_id TEXT,
    connector_id TEXT,
    connector_revision TEXT,
    external_account_id TEXT,
    credential_reference TEXT,             -- internal reference, NEVER a raw secret
    target TEXT NOT NULL DEFAULT '',
    canonical_arguments_json TEXT NOT NULL DEFAULT '{}',
    request_digest TEXT NOT NULL,
    approval_reference TEXT,
    state TEXT NOT NULL DEFAULT 'PREPARED',
    state_version INTEGER NOT NULL DEFAULT 1,
    provider_idempotency_key TEXT,
    compensates_effect_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_effects_digest
ON effects(owner_user_id, operation_name, request_digest) WHERE state != 'CANCELLED_BEFORE_DISPATCH';
CREATE INDEX IF NOT EXISTS idx_effects_run ON effects(run_id);
CREATE INDEX IF NOT EXISTS idx_effects_state ON effects(state);

CREATE TABLE IF NOT EXISTS effect_receipts (
    receipt_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL,
    effect_attempt_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_account_scope TEXT,
    provider_request_id TEXT,
    provider_operation_id TEXT,
    remote_resource_id TEXT,
    provider_status TEXT NOT NULL,
    accepted_at INTEGER,
    observed_at INTEGER NOT NULL,
    response_digest TEXT,
    evidence_reference TEXT,
    receipt_schema_version INTEGER NOT NULL DEFAULT 1,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_receipts_effect ON effect_receipts(effect_id);

-- ── Atomic schedule occurrence claims (Phase 3 §3.15) ────────────
-- claim(schedule_id, occurrence) is an atomic INSERT: two workers racing
-- to start the same occurrence — one winner, one explicit SKIPPED_BUSY.
CREATE TABLE IF NOT EXISTS schedule_occurrences (
    schedule_id TEXT NOT NULL,
    occurrence TEXT NOT NULL,              -- stable key: ISO scheduled time
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'claimed',-- claimed|started|skipped_busy|completed
    claimed_at INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, occurrence)
);

-- ── MCP Tool Cache ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mcp_tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    description TEXT NOT NULL,
    input_schema TEXT NOT NULL,              -- JSON
    mcp_id TEXT NOT NULL REFERENCES mcps(mcp_id) ON DELETE CASCADE,
    mcp_version TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    auth_requirements TEXT NOT NULL DEFAULT '[]',  -- JSON
    risk_level INTEGER NOT NULL DEFAULT 4          -- RiskLevel.CRITICAL
);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_mcp ON mcp_tools(mcp_id);

-- ── Memories (Vector Store) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'semantic',  -- legacy: 'profile' | 'workflow' | 'semantic'
    metadata TEXT NOT NULL DEFAULT '{}',            -- JSON
    embedding TEXT NOT NULL,                        -- JSON array of floats
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- Phase 16 §16.1: memory classification.
    memory_class TEXT NOT NULL DEFAULT 'factual',
    -- transient_context | execution_episode | procedure | factual | preference
    -- Phase 16 §16.2: provenance + freshness.
    source TEXT NOT NULL DEFAULT '',
    confidence REAL,
    expires_at INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    -- Phase 16 §16.4: conflicts — superseded history is PRESERVED.
    subject_key TEXT,
    superseded_by TEXT,
    -- Phase 16 v37 (§16.1): the EXPLICIT sharing scope — a personal
    -- memory is personal until an authorized sharing operation changes
    -- its scope; org membership NEVER implies visibility.
    scope TEXT NOT NULL DEFAULT 'personal',
    -- task | workflow | personal | project | organization | published
    scope_ref TEXT NOT NULL DEFAULT '',
    -- §16.2: verification status + sensitivity + observation time.
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    -- confirmed | inferred | disputed | unverified
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    -- normal | sensitive | secret_ref
    observed_at INTEGER,
    -- §16.2/§16.5: extraction metadata + source-event identity.
    extraction_meta TEXT NOT NULL DEFAULT '{}',
    source_event_id TEXT,
    -- §16.22: deletion is a WORKFLOW (marker first, cleanup observable).
    deletion_state TEXT NOT NULL DEFAULT 'active',
    -- active | deletion_requested | hidden | cleanup_complete
    UNIQUE(user_id, source_event_id)
);
-- ── Phase 18 C7/C8: durable publication jobs ───────────────────
-- Publication progress lives in the DURABLE record (owner, lease,
-- stage, attempts, error) — not a local dataclass. A crash at any
-- stage leaves a claimable, retryable job; the reconciler EXECUTES
-- recovery through this contract.
CREATE TABLE IF NOT EXISTS publication_jobs (
    job_id TEXT PRIMARY KEY,
    deliverable_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'generation_allocated',
    state TEXT NOT NULL DEFAULT 'pending',
    -- pending | claimed | complete | failed
    owner_worker_id TEXT,
    lease_expires_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    fail_after TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publication_jobs_pending
ON publication_jobs(state, created_at);

-- ── Phase 17 §17.6: the durable deliverable contract ───────────
-- Restart-readable records: what was produced, by which task, from
-- which sources, with which verification evidence. The EXISTING
-- artifact builder stays the producer; this is the delivery record.
CREATE TABLE IF NOT EXISTS deliverables (
    deliverable_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    parent_revision INTEGER,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    run_id TEXT,
    task_id TEXT,
    deliverable_type TEXT NOT NULL,
    -- resume_html | resume_pdf | factual_report | source_project | tested_project
    mime_type TEXT NOT NULL DEFAULT '',
    file_manifest TEXT NOT NULL DEFAULT '[]',
    -- [{path, byte_length, sha256}]
    content_checksum TEXT NOT NULL DEFAULT '',
    lineage TEXT NOT NULL DEFAULT '{}',
    -- {source_memory_ids, generator_version, template_version, source_revisions}
    verification_json TEXT NOT NULL DEFAULT '{}',
    -- {verdict, verifier_version, checks, evidence_refs, verified_at}
    lifecycle_state TEXT NOT NULL DEFAULT 'generated',
    -- generated | stored | verification_completed | verification_passed
    -- | verification_failed | review_completed | delivered | deleted
    access_policy TEXT NOT NULL DEFAULT 'owner_only',
    deliverable_key TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(deliverable_key, revision)
);
CREATE TABLE IF NOT EXISTS deliverable_keys (
    deliverable_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

-- ── Phase 17 §17.4: durable memory lifecycle ────────────────────
-- Dependency edges PERSIST (a restart must not erase that a report,
-- cache, or dataset depended on a memory) and cleanup jobs are
-- claimable, retryable work — not in-process side effects.
CREATE TABLE IF NOT EXISTS memory_dependency_edges (
    edge_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    derived_kind TEXT NOT NULL,
    derived_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(memory_id, derived_kind, derived_id)
);
CREATE TABLE IF NOT EXISTS memory_cleanup_jobs (
    job_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    -- pending | claimed | complete | failed
    owner_worker_id TEXT,
    lease_expires_at INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    dependents_json TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- (the governed indexes are created in _migrate_memory_governance_columns —
--  inside SCHEMA_SQL they would fail on pre-existing databases whose
--  memories table predates the governed columns)

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_type ON memories(user_id, memory_type);

-- ── Secrets (Encrypted Vault) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS secrets (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,           -- AES-256-GCM ciphertext (base64)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

-- ── Resumes ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS resumes (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT 'Master Resume',
    is_master INTEGER NOT NULL DEFAULT 0,
    schema_json TEXT NOT NULL DEFAULT '{}',  -- JSON canonical schema
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resumes_user ON resumes(user_id);

CREATE TABLE IF NOT EXISTS resume_versions (
    id TEXT PRIMARY KEY,
    resume_id TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    job_description TEXT,
    tailored_schema TEXT NOT NULL DEFAULT '{}',  -- JSON
    ats_score REAL,
    ats_breakdown TEXT,                          -- JSON scoring details
    format TEXT NOT NULL DEFAULT 'pdf',
    file_path TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resume_versions_resume ON resume_versions(resume_id);

-- ── Schedules ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    cron_expression TEXT,
    schedule_type TEXT NOT NULL DEFAULT 'one_time',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    missed_run_policy TEXT NOT NULL DEFAULT 'coalesce_one',  -- §29.19: skip|first|second|coalesce_one
    status TEXT NOT NULL DEFAULT 'active',
    next_run_at TEXT,                       -- canonical UTC instant
    next_run_local TEXT,                    -- logical local wall-clock occurrence
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_user ON schedules(user_id);

CREATE TABLE IF NOT EXISTS schedule_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id TEXT NOT NULL REFERENCES schedules(schedule_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    triggered_at TEXT NOT NULL
);

-- ── Plugins ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS plugins (
    id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,             -- JSON PluginManifest
    state TEXT NOT NULL DEFAULT 'installed',
    installed_by TEXT,
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ── Audit Log ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    workflow_id TEXT,
    run_id TEXT,
    task_id TEXT,
    trace_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',      -- JSON
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_resource ON audit_logs(resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id);

-- ── Notifications ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',     -- JSON (e.g. workflow_id, approval_id)
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);

-- ── Idempotency Ledger ───────────────────────────────────────────
-- ── Durable Usage Ledger (audit F08) ────────────────────────────
-- Shared per-user daily token accounting; the process-local ContextManager
-- dict is only a fast cache on top of this.
CREATE TABLE IF NOT EXISTS usage_ledger (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);

-- ── Phase 2 budget accounting (docs/budget-accounting.md §10) ───
-- Per-bucket aggregate view (scope, subject, UTC period). Transactionally
-- maintained alongside budget_reservations/budget_events; checked against
-- them by the reconciliation tests. `legacy_seed` is the portion of the
-- usage_ledger row already mirrored into `consumed` (§2.18 migration), so
-- pre-Phase-2 aggregates count exactly once.
CREATE TABLE IF NOT EXISTS budget_buckets (
    scope TEXT NOT NULL,                     -- platform | org | user | run | call
    subject_id TEXT NOT NULL,
    period TEXT NOT NULL,                    -- UTC day 'YYYY-MM-DD'
    unit TEXT NOT NULL DEFAULT 'tokens',
    consumed INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    uncertain_reserved INTEGER NOT NULL DEFAULT 0,
    legacy_seed INTEGER NOT NULL DEFAULT 0,
    limit_applied INTEGER,
    policy_version TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, subject_id, period, unit)
);

-- Individual durable reservation holds (§2.5). One row per potentially
-- billable provider attempt; `request_key` makes repeated admission
-- idempotent and `record_version` guards optimistic state transitions.
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    user_id TEXT NOT NULL,
    organization_id TEXT,
    run_id TEXT,
    task_id TEXT,
    call_id TEXT,
    attempt_id TEXT,
    provider TEXT NOT NULL DEFAULT 'gemini',
    model TEXT,
    credential_owner_reference TEXT,
    purpose TEXT NOT NULL DEFAULT 'generation',
    unit TEXT NOT NULL DEFAULT 'tokens',
    units INTEGER NOT NULL,                  -- original hold
    settled_units INTEGER,
    state TEXT NOT NULL DEFAULT 'reserved',
    period TEXT NOT NULL,
    policy_version TEXT,
    pricing_version TEXT,
    buckets_json TEXT NOT NULL DEFAULT '[]', -- scopes the hold was charged to
    receipt_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    dispatch_started_at TEXT,
    provider_request_id TEXT,
    measurement_source TEXT NOT NULL DEFAULT 'estimated',
    record_version INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_budget_res_user_period ON budget_reservations(user_id, period);
CREATE INDEX IF NOT EXISTS idx_budget_res_state ON budget_reservations(state, updated_at);

-- Append-only accounting events (§2.7/§2.19): reservation, dispatch,
-- settlement, release, uncertainty, reconciliation, authorized corrections.
CREATE TABLE IF NOT EXISTS budget_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    reservation_id TEXT,
    scope TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    period TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'tokens',
    units INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_budget_events_res ON budget_events(reservation_id);
CREATE INDEX IF NOT EXISTS idx_budget_events_subject ON budget_events(scope, subject_id, period);

CREATE TABLE IF NOT EXISTS idempotency_ledger (
    idempotency_key TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'completed' | 'failed'
    result_payload TEXT,                     -- JSON cache of successful output
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_kind_created ON content_records(kind, created_at);
"""


# ═══════════════════════════════════════════════════════════════════
#  DATABASE MANAGER
# ═══════════════════════════════════════════════════════════════════

class DatabaseManager:
    """
    Async SQLite connection manager.
    
    Usage:
        db = DatabaseManager()
        await db.initialize()
        
        async with db.connection() as conn:
            cursor = await conn.execute("SELECT ...")
            rows = await cursor.fetchall()
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or settings.SQLITE_DB_PATH
        self._connection: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create the database directory, open connection, enable WAL, run migrations."""
        # Ensure data directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            Path(db_dir).mkdir(parents=True, exist_ok=True)

        # Open connection
        self._connection = await aiosqlite.connect(self.db_path)

        # Enable WAL mode for better concurrent read performance
        await self._connection.execute("PRAGMA journal_mode=WAL")
        # Enable foreign keys
        await self._connection.execute("PRAGMA foreign_keys=ON")
        # Row factory to return dicts
        self._connection.row_factory = aiosqlite.Row

        # Run schema migration
        await self._run_migrations()

        logger.info(f"SQLite database initialized at {self.db_path}")

    async def _run_migrations(self) -> None:
        """Create all tables if they don't exist. Idempotent."""
        await self._connection.executescript(SCHEMA_SQL)
        await self._migrate_rate_limit_schema()
        await self._migrate_command_columns()
        await self._migrate_task_deferral_columns()
        await self._migrate_connector_phase6()
        await self._migrate_browser_sessions()
        await self._migrate_task_criteria()
        await self._migrate_mcp_columns()
        await self._migrate_workflow_thread_columns()
        await self._migrate_task_columns()
        await self._migrate_mcp_build_columns()
        await self._migrate_schedule_occurrence_columns()
        await self._migrate_usage_columns()
        await self._migrate_user_identity_columns()
        await self._migrate_evaluation_records()
        await self._migrate_memory_lifecycle()
        await self._migrate_experiment_manifest()
        await self._migrate_agent_task_contract_columns()
        await self._migrate_memory_governance_columns()
        await self._connection.commit()
        logger.info("Database schema migration complete")

    async def _migrate_schedule_occurrence_columns(self) -> None:
        """§29.19 revision: the missed-run policy and the logical local
        occurrence travel with the schedule (additive)."""
        cursor = await self._connection.execute("PRAGMA table_info(schedules)")
        cols = {row[1] for row in await cursor.fetchall()}
        for column, ddl in (
                ("missed_run_policy",
                 "TEXT NOT NULL DEFAULT 'coalesce_one'"),
                ("next_run_local", "TEXT")):
            if column not in cols:
                await self._connection.execute(
                    f"ALTER TABLE schedules ADD COLUMN {column} {ddl}")

    async def _migrate_memory_governance_columns(self) -> None:
        """Phase 16 v37: the governed-memory record columns."""
        cursor = await self._connection.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in await cursor.fetchall()}
        additions = {
            "scope": "TEXT NOT NULL DEFAULT 'personal'",
            "scope_ref": "TEXT NOT NULL DEFAULT ''",
            "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "sensitivity": "TEXT NOT NULL DEFAULT 'normal'",
            "observed_at": "INTEGER",
            "extraction_meta": "TEXT NOT NULL DEFAULT '{}'",
            "source_event_id": "TEXT",
            "deletion_state": "TEXT NOT NULL DEFAULT 'active'",
        }
        for name, decl in additions.items():
            if name not in cols:
                await self._connection.execute(
                    f"ALTER TABLE memories ADD COLUMN {name} {decl}")
        # The governed indexes — created HERE (after the ALTERs) so
        # pre-existing databases migrate safely.
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_scope "
            "ON memories(user_id, scope, memory_class)")
        # The source-event identity: a partial unique index (only
        # non-null) — event replay cannot multiply memories.
        # §17.4: the identity is (user_id, source_event_id) — the same
        # event id under DIFFERENT owners is not a duplicate.
        await self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_source_event_uniq "
            "ON memories(user_id, source_event_id) "
            "WHERE source_event_id IS NOT NULL")

    async def _migrate_agent_task_contract_columns(self) -> None:
        """§15.3: the complete delegation-contract columns."""
        cursor = await self._connection.execute(
            "PRAGMA table_info(agent_tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        additions = {
            "goal": "TEXT NOT NULL DEFAULT ''",
            "exclusions_json": "TEXT NOT NULL DEFAULT '[]'",
            "input_refs_json": "TEXT NOT NULL DEFAULT '[]'",
            "required_capabilities_json": "TEXT NOT NULL DEFAULT '[]'",
            "acceptance_criteria_json": "TEXT NOT NULL DEFAULT '[]'",
            "retry_policy": "TEXT NOT NULL DEFAULT 'bounded'",
            "agent_version": "TEXT NOT NULL DEFAULT ''",
            "tool_version": "TEXT NOT NULL DEFAULT ''",
            "policy_version": "TEXT NOT NULL DEFAULT ''",
        }
        for name, decl in additions.items():
            if name not in cols:
                await self._connection.execute(
                    f"ALTER TABLE agent_tasks ADD COLUMN {name} {decl}")

    async def _migrate_experiment_manifest(self) -> None:
        """§13.10: the pinned manifest column on experiments."""
        cursor = await self._connection.execute("PRAGMA table_info(experiments)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "manifest_json" not in cols:
            await self._connection.execute(
                "ALTER TABLE experiments ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}'")

    async def _migrate_memory_lifecycle(self) -> None:
        """Phase 16 §16.1–16.4: memory classification, provenance, and
        conflict columns on the memories table."""
        cursor = await self._connection.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in await cursor.fetchall()}
        if not cols:
            return
        additions = {
            "memory_class": "TEXT NOT NULL DEFAULT 'factual'",
            "source": "TEXT NOT NULL DEFAULT ''",
            "confidence": "REAL",
            "expires_at": "INTEGER",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "subject_key": "TEXT",
            "superseded_by": "TEXT",
        }
        for col, ddl in additions.items():
            if col not in cols:
                await self._connection.execute(
                    f"ALTER TABLE memories ADD COLUMN {col} {ddl}")

    async def _migrate_evaluation_records(self) -> None:
        """§12.9/§12.11: measurement columns become NULLABLE (unavailable
        ≠ zero), the previous_evaluation_id linkage column is added, and
        the (run_id, evaluator_version) identity index is created after
        collapsing any pre-migration duplicates (earliest kept)."""
        cursor = await self._connection.execute(
            "PRAGMA table_info(evaluation_records)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        if not cols:
            return   # fresh schema created it correctly
        if "previous_evaluation_id" not in cols:
            try:
                await self._connection.execute(
                    "ALTER TABLE evaluation_records "
                    "ADD COLUMN previous_evaluation_id TEXT")
            except Exception:
                pass
        needs_rebuild = any(
            name in cols and cols[name][3]   # notnull flag
            for name in ("token_usage_total", "latency_ms",
                         "unknown_tool_call_rate"))
        if needs_rebuild:
            await self._connection.execute(
                "CREATE TABLE evaluation_records_new ("
                "evaluation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "workflow_id TEXT, agent_name TEXT, model TEXT, task_type TEXT, "
                "success INTEGER NOT NULL DEFAULT 0, "
                "recovery_attempts INTEGER NOT NULL DEFAULT 0, "
                "successful_recoveries INTEGER NOT NULL DEFAULT 0, "
                "tool_calls INTEGER NOT NULL DEFAULT 0, "
                "unknown_tool_call_rate REAL, invalid_argument_rate REAL, "
                "tool_schema_violation_rate REAL, latency_ms REAL, "
                "token_usage_total INTEGER, token_usage_input INTEGER, "
                "token_usage_output INTEGER, "
                "evaluator_version TEXT NOT NULL DEFAULT '1.0.0', "
                "prompt_version TEXT NOT NULL DEFAULT '1.0.0', "
                "fixture_hash TEXT, "
                "effect_states_json TEXT NOT NULL DEFAULT '{}', "
                "criterion_results_json TEXT NOT NULL DEFAULT '[]', "
                "evidence_references_json TEXT NOT NULL DEFAULT '[]', "
                "confidence_limitations_json TEXT NOT NULL DEFAULT '[]', "
                "previous_evaluation_id TEXT, "
                "created_at_epoch INTEGER NOT NULL, "
                "record_json TEXT NOT NULL DEFAULT '{}')")
            await self._connection.execute(
                "INSERT OR IGNORE INTO evaluation_records_new "
                "SELECT evaluation_id, run_id, workflow_id, agent_name, model, "
                "task_type, success, recovery_attempts, successful_recoveries, "
                "tool_calls, unknown_tool_call_rate, invalid_argument_rate, "
                "tool_schema_violation_rate, latency_ms, token_usage_total, "
                "token_usage_input, token_usage_output, evaluator_version, "
                "prompt_version, fixture_hash, effect_states_json, "
                "criterion_results_json, evidence_references_json, "
                "confidence_limitations_json, previous_evaluation_id, "
                "created_at_epoch, record_json FROM evaluation_records "
                "ORDER BY created_at_epoch ASC")
            await self._connection.execute("DROP TABLE evaluation_records")
            await self._connection.execute(
                "ALTER TABLE evaluation_records_new RENAME TO evaluation_records")
        await self._connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_evaluations_identity "
            "ON evaluation_records(run_id, evaluator_version)")

    async def _migrate_task_criteria(self) -> None:
        """Phase 10 §10.7: acceptance-criteria columns on tasks."""
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        additions = {
            "acceptance_criteria_json": "TEXT NOT NULL DEFAULT '[]'",
            "requested_outcomes_json": "TEXT NOT NULL DEFAULT '[]'",
            "allowed_action_scope_json": "TEXT NOT NULL DEFAULT '[]'",
            "completion_policy": "TEXT NOT NULL DEFAULT 'criteria'",
        }
        for col, ddl in additions.items():
            if col not in cols:
                await self._connection.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        if additions:
            logger.info("Migrated tasks with §10.7 criteria columns")

    async def _migrate_browser_sessions(self) -> None:
        """Phase 9 §9.0A: server-controlled fencing generation."""
        cursor = await self._connection.execute("PRAGMA table_info(browser_sessions)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "owner_generation" not in cols:
            await self._connection.execute(
                "ALTER TABLE browser_sessions ADD COLUMN owner_generation INTEGER NOT NULL DEFAULT 0")
            logger.info("Migrated browser_sessions with owner_generation")
        if cols and "controller_generation" not in cols:
            await self._connection.execute(
                "ALTER TABLE browser_sessions ADD COLUMN controller_generation INTEGER NOT NULL DEFAULT 0")

    async def _migrate_connector_phase6(self) -> None:
        """Phase 6 §6.14/§6.8: active revision pointer + validation runs."""
        cursor = await self._connection.execute("PRAGMA table_info(mcps)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "active_revision_id" not in cols:
            await self._connection.execute(
                "ALTER TABLE mcps ADD COLUMN active_revision_id TEXT")
            logger.info("Migrated mcps with active_revision_id")

    async def _migrate_task_deferral_columns(self) -> None:
        """Phase 4 §4.11: persisted retry/deferral state — a restart cannot
        erase the timer because the RECONCILER wakes due tasks from the row,
        not from process-local asyncio timers."""
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "next_attempt_at" not in cols:
            await self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN next_attempt_at TEXT")
            await self._connection.execute(
                "ALTER TABLE tasks ADD COLUMN defer_count INTEGER NOT NULL DEFAULT 0")
            logger.info("Migrated tasks with durable deferral columns")

    async def _migrate_command_columns(self) -> None:
        """Phase 4 §4.0D (R8): payload digests for idempotency conflict
        detection. Additive; existing rows keep NULL digests."""
        cursor = await self._connection.execute("PRAGMA table_info(pending_commands)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "request_digest" not in cols:
            await self._connection.execute(
                "ALTER TABLE pending_commands ADD COLUMN request_digest TEXT")
            logger.info("Migrated pending_commands with request_digest column")

    async def _migrate_rate_limit_schema(self) -> None:
        """Phase 3 §3.4: pre-Phase-3 databases stored a window ORDINAL in
        window_start and had no window_seconds/cleanup_after columns. Those
        rows are ephemeral rate counters (not user data), so the table is
        recreated in the epoch representation; in-flight windows lose their
        counts once, at upgrade, which relaxes limits for ≤ one window."""
        cursor = await self._connection.execute("PRAGMA table_info(rate_limits)")
        cols = {row[1] for row in await cursor.fetchall()}
        if cols and "window_seconds" not in cols:
            await self._connection.execute("DROP TABLE rate_limits")
            await self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rate_limits (
                    identifier TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    window_seconds INTEGER NOT NULL,
                    window_start INTEGER NOT NULL,
                    window_end INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    cleanup_after INTEGER NOT NULL,
                    policy_version TEXT NOT NULL DEFAULT 'p3.v1',
                    PRIMARY KEY (identifier, group_name, window_seconds, window_start)
                );
                CREATE INDEX IF NOT EXISTS idx_rate_limits_cleanup
                    ON rate_limits(cleanup_after);
                """
            )
            logger.info("Migrated rate_limits to Phase 3 epoch-window representation")

    async def _migrate_mcp_columns(self) -> None:
        """Add columns introduced after the original schema."""
        cursor = await self._connection.execute("PRAGMA table_info(mcps)")
        existing = {row[1] for row in await cursor.fetchall()}
        additions = {
            "description": "TEXT NOT NULL DEFAULT ''",
            "trust_tier": "TEXT NOT NULL DEFAULT 'pending_review'",
            "source_type": "TEXT NOT NULL DEFAULT 'openapi'",
            "spec_json": "TEXT",
            "is_public": "INTEGER NOT NULL DEFAULT 0",
            "artifact_type": "TEXT NOT NULL DEFAULT 'adapter'",
            "spec_provenance": "TEXT NOT NULL DEFAULT 'unknown'",
            "test_coverage_json": "TEXT NOT NULL DEFAULT '{}'",
            "capabilities_omitted": "INTEGER NOT NULL DEFAULT 0",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for col, ddl in additions.items():
            if col not in existing:
                await self._connection.execute(f"ALTER TABLE mcps ADD COLUMN {col} {ddl}")

        cursor = await self._connection.execute("PRAGMA table_info(mcp_tools)")
        tool_cols = {row[1] for row in await cursor.fetchall()}
        if "operation_json" not in tool_cols:
            await self._connection.execute(
                "ALTER TABLE mcp_tools ADD COLUMN operation_json TEXT NOT NULL DEFAULT '{}'"
            )

    async def _migrate_workflow_thread_columns(self) -> None:
        cursor = await self._connection.execute("PRAGMA table_info(workflow_runs)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "parent_run_id" not in cols:
            await self._connection.execute("ALTER TABLE workflow_runs ADD COLUMN parent_run_id TEXT")
        if "thread_id" not in cols:
            await self._connection.execute("ALTER TABLE workflow_runs ADD COLUMN thread_id TEXT")
        # Explicit organization ownership (audit F01): NULL means personal.
        # Membership alone never grants oversight access — only runs created
        # with an explicit org_id are organization-owned.
        if "org_id" not in cols:
            await self._connection.execute("ALTER TABLE workflow_runs ADD COLUMN org_id TEXT")
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_thread ON workflow_runs(user_id, thread_id)"
        )
        await self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_org ON workflow_runs(org_id)"
        )

    async def _migrate_user_identity_columns(self) -> None:
        cursor = await self._connection.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cursor.fetchall()}
        # Phase 1.3 identity fields: verified_email for invitation acceptance;
        # session_version for bounded-latency revocation (reserved for use).
        if "email_verified" not in cols:
            await self._connection.execute(
                "ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        if "session_version" not in cols:
            await self._connection.execute(
                "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")

    async def _migrate_usage_columns(self) -> None:
        cursor = await self._connection.execute("PRAGMA table_info(usage_ledger)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "reserved" not in cols:
            # Atomic budget reservations (v5.6 review blocker A3).
            await self._connection.execute("ALTER TABLE usage_ledger ADD COLUMN reserved INTEGER NOT NULL DEFAULT 0")

    async def _migrate_mcp_build_columns(self) -> None:
        cursor = await self._connection.execute("PRAGMA table_info(mcp_builds)")
        cols = {row[1] for row in await cursor.fetchall()}
        # Build provenance (audit G07): which surface asked for the build and
        # which run is waiting on it.
        for column in ("origin", "parent_run_id", "wait_reason", "parent_workflow_id", "parent_task_id"):
            if column not in cols:
                await self._connection.execute(f"ALTER TABLE mcp_builds ADD COLUMN {column} TEXT")
        # §12.6: the build carries its organization scope and the
        # credential-BINDING REFERENCE (never a raw secret) end to end.
        for column in ("organization_id", "credential_reference"):
            if column not in cols:
                await self._connection.execute(f"ALTER TABLE mcp_builds ADD COLUMN {column} TEXT")
        # §13.3: the declared required operations travel with the build.
        if "required_operations_json" not in cols:
            await self._connection.execute(
                "ALTER TABLE mcp_builds ADD COLUMN required_operations_json TEXT NOT NULL DEFAULT '[]'")
        # §29.14: the application-intake record — provenance, digest,
        # documented interfaces and auth requirements persist BEFORE any
        # generation runs.
        if "intake_json" not in cols:
            await self._connection.execute(
                "ALTER TABLE mcp_builds ADD COLUMN intake_json TEXT NOT NULL DEFAULT '{}'")

    async def _migrate_task_columns(self) -> None:
        cursor = await self._connection.execute("PRAGMA table_info(tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "title" not in cols:
            await self._connection.execute("ALTER TABLE tasks ADD COLUMN title TEXT")

    async def connection(self) -> aiosqlite.Connection:
        """Get the database connection. Raises if not initialized."""
        if self._connection is None:
            raise RuntimeError(
                "Database not initialized. Call await db.initialize() first."
            )
        return self._connection

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute a single SQL statement."""
        conn = await self.connection()
        return await conn.execute(sql, params)

    async def execute_insert(self, sql: str, params: tuple = ()) -> Optional[int]:
        """Execute an INSERT and return lastrowid."""
        conn = await self.connection()
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor.lastrowid

    async def execute_many(self, sql: str, params_list: list) -> None:
        """Execute a statement with multiple parameter sets."""
        conn = await self.connection()
        await conn.executemany(sql, params_list)
        await conn.commit()

    async def fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Fetch a single row as a dict."""
        conn = await self.connection()
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, params: tuple = ()) -> list:
        """Fetch all rows as a list of dicts."""
        conn = await self.connection()
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        """Commit the current transaction."""
        conn = await self.connection()
        await conn.commit()

    @asynccontextmanager
    async def write_transaction(self, max_begin_attempts: int = 4) -> AsyncIterator[aiosqlite.Connection]:
        """An exclusively owned write transaction on a DEDICATED connection.

        Budget admission/settlement must never interleave with unrelated work,
        so these run on their own connection (the shared one is untouched) in
        autocommit mode with an explicit BEGIN IMMEDIATE: write ownership is
        acquired before the read/modify sequence, so two processes racing near
        the cap cannot both pass a check-then-write. Contention surfaces as
        SQLITE_BUSY — handled by bounded retry with clear failure, never by
        silently dropping the write.
        """
        conn = await aiosqlite.connect(self.db_path, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA foreign_keys=ON")
            for attempt in range(max_begin_attempts):
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    break
                except aiosqlite.OperationalError as exc:
                    locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
                    if not locked or attempt == max_begin_attempts - 1:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
            yield conn
            await conn.commit()
        except BaseException:
            try:
                await conn.execute("ROLLBACK")
            except aiosqlite.Error:
                pass  # no active transaction (BEGIN never succeeded)
            raise
        finally:
            await conn.close()

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
            logger.info("SQLite database connection closed")


# ── Singleton instance ────────────────────────────────────────────
# Created once, shared across the app via the repository factory.
db_manager = DatabaseManager()
