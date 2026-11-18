# Full-stack implementation roadmap

Planning baseline: 31 October 2026; committed source `3ef8063` (v5.5.0), plus
working-tree changes observed during review. This is an implementation plan,
not a completion report. No application implementation is supplied by this
document. Existing features should be extended, not rebuilt without evidence.

The product goal is an autonomous workspace that understands an outcome,
uses or builds supported tools, executes authorized work, recovers safely,
and delivers verifiable artifacts. An arbitrary URL does not guarantee an
available API, usable credentials, supported browser workflow, or permission.

## How to execute this plan

Paths are relative to the repository root. **Edit** identifies existing files;
**Add** identifies proposed files that do not need to exist yet. Proposed
services, routes, tables, and event types are design contracts, not current APIs.
Check the working tree before implementation: another session may already have
started a fix. Keep each change reviewable and include its migration and tests.

Each phase closes only when its acceptance checks pass on the named commit and
environment. Record logs, image digests, exclusions, and rollback instructions in
[RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md). A test count is not a maturity score.
Schedule by dependency and measured effort after discovery, not invented dates.

| Phase | Outcome | Dependencies | Release relevance |
|---|---|---|---|
| 00 | Accurate scope and evidence | None | Required |
| 01 | Identity, membership, tenant boundaries | 00 | Required |
| 02 | Atomic spending control | 00–01 | Required for paid execution |
| 03 | Distributed abuse controls | 01–02 | Required for public scale |
| 04 | Durable commands and workflow recovery | 01–02 | Required |
| 05 | Side-effect reconciliation | 04 | Required for external writes |
| 06 | Trustworthy connector lifecycle | 01, 04–05 | Required for connector scope |
| 07 | Isolated executable tools | 06 | Required before generated-code execution |
| 08 | Managed browser runtime | 01, 04–05 | Required for browser scope |
| 09 | Reliable DOM and browser actions | 08 | Required for supported browser journeys |
| 10 | Verified ReAct execution | 02, 04–06, 09 | Advanced execution |
| 11 | Bounded self-healing | 05–10 | Reliability |
| 12 | Outcome verification and evaluations | 04–06 | Required before learning claims |
| 13 | Controlled self-improvement | 11–12 | Advanced learning |
| 14 | Contextual bandits and RL research | 02, 12–13 | Optional learning expansion |
| 15 | Specialized subagents | 10, 12 | Optional capability expansion |
| 16 | Governed memory and retrieval | 01, 12 | Required for shared-memory scope |
| 17 | Provenance-backed deliverables | 01, 05, 12 | Required for deliverable scope |
| 18 | React state and interaction reliability | 00–01; consume later contracts | Required frontend baseline |
| 19 | Accessible design and useful 3D | 18 | Polish; accessibility is required |
| 20 | Release operations and assurance | Applicable required phases | Required before launch |
| 21 | Product and enterprise expansion | 20 plus relevant capabilities | Optional, scope-driven |

Phases 18–19 can progress alongside backend work after API contracts are agreed.
Start observability and test infrastructure early; phase 20 is the final evidence
gate, not permission to postpone instrumentation. Do not require every optional
phase to launch a deliberately limited, tested product.

## Phase 00 — Establish an honest baseline

**Goal:** give every claim a source, environment, and acceptance criterion.

**Edit:** `README.md`, `docs/RELEASE_EVIDENCE.md`,
`docs/production-readiness.md`, `docs/implementation-status.md`,
`scripts/verify-local.sh`, `cloudbuild.yaml`, `cloudbuild.frontend.yaml`.
**Add:** `docs/release-scope.md` and `docs/evidence/README.md` when execution starts.

1. Choose supported launch journeys and explicitly excluded capabilities.
2. Map F01–F16 and G01–G08 to reproducible checks; reopen incomplete findings.
3. Capture commit, dirty-tree state, Python/Node versions, dependency resolution,
   test environment, and image digests. Keep old results labeled historical.
4. Split mocked unit tests, local integration tests, real-backend browser tests,
   provider tests, and deployed failure drills. Never combine their guarantees.
5. Associate one accountable owner with each gate and list required credentials
   or infrastructure without copying secrets into evidence.

**Current delta:** the legacy membership route has an uncommitted invitation-only
change, and SQLite usage recording has an uncommitted commit call. Validate both
before closing their findings. Shared-connection transactions still need review.

**Acceptance:** every supported journey has a test and failure expectation;
documentation contains no unsupported blanket completion/certification claim.
**Rollout:** documentation first; no production behavior changes. Update evidence
after each subsequent phase rather than only at the end.

## Phase 01 — Identity, membership, and tenant isolation

**Goal:** membership requires acceptance and never implicitly shares personal data.

**Edit:** `backend/api/routers/orgs.py`, `backend/api/dependencies/auth.py`,
`backend/api/routers/auth.py`, `backend/repositories/sqlite/org_repository.py`,
`backend/repositories/firestore/sidecar.py`, `backend/models/schemas.py`,
`backend/repositories/sqlite/database.py`, `frontend/src/lib/api.ts`,
`frontend/src/app/dashboard/orgs/page.tsx`.
**Add:** `backend/services/resource_authorization.py` if shared authorization
rules cannot be expressed cleanly in existing dependencies.

1. Make legacy and new invitation routes use one implementation and response
   contract. Preserve owner/admin restrictions and existing-member behavior.
2. Enforce pending-invite uniqueness transactionally. Check email, expiry,
   revocation, organization existence, and membership within acceptance.
3. Make accept/revoke concurrency linearizable. Use explicit transaction ownership
   in SQLite; do not allow unrelated async work to commit another operation.
4. Define personal versus organization ownership for every resource. Authorize
   creation, queries, streams, exports, and mutations, including indirect IDs.
5. Define disable/re-enable, session revocation, API-key revocation, and credential
   access behavior; test real HTTP paths with current account state.
6. Provide invitation status, revoke/resend, role descriptions, and clear personal
   workspace separation in the UI. Avoid showing secrets in notification history.

**Acceptance:** extend `test_org_invitations.py`, `test_orgs_rbac.py`,
`test_auth_http.py`, and `test_audit_security.py`; add separate-connection races
and Firestore-emulator tests. Test cross-user reads/writes for every resource.
Use `frontend/e2e/orgs.spec.ts` against a real test backend as well as mocks.
**Rollout:** migrate ownership conservatively; never infer historical sharing from
membership. Update `docs/SECURITY.md` and organization user guidance.

## Phase 02 — Atomic budgets and complete usage accounting

**Goal:** concurrent paid calls cannot spend the same available allowance.

**Edit:** `backend/repositories/usage_repository.py`,
`backend/services/llm_context.py`, `backend/services/gemini_client.py`,
`backend/services/groq_adk.py`, `backend/services/huggingface_adk.py`,
`backend/engine/engine.py`, `backend/repositories/factory.py`,
`backend/repositories/sqlite/database.py`, `backend/api/routers/usage.py`,
`frontend/src/components/ContextUsage.tsx`,
`frontend/src/app/dashboard/settings/page.tsx`.
**Add:** `backend/services/budget_service.py`,
`backend/models/usage.py`, `backend/tests/test_budget_reservations.py`.

1. Define reservation IDs bound to tenant, user, run, task, provider, model, and
   attempt. Separate estimated tokens, measured tokens, and estimated currency.
2. Atomically check `consumed + active reservations + requested <= limit` before
   each paid call. Apply output-token bounds and versioned price assumptions.
3. Settle from provider usage; release only demonstrably unused reservations.
   Retain uncertain reservations for reconciliation after a crash.
4. Account for failed calls, fallback models, embeddings, browser decisions,
   recovery, integration creation, scheduled work, and ADK calls.
5. Replace untracked fire-and-forget writes with durable completion accounting.
   Treat unavailable accounting explicitly; do not silently grant free capacity.
6. Show available, reserved, consumed, and uncertain usage separately in the UI.

**Acceptance:** separate processes contend for one small budget without exceeding
the reservation rule; usage survives restart; settlement is idempotent; midnight
boundaries and reservation expiry cannot erase incurred usage. Test both stores.
**Rollout:** begin with shadow accounting, compare measurements, then enforce a
documented limit. Provider invoice reconciliation is a separate later check, not
a reason to defer implementing local reservations. Update usage documentation.

## Phase 03 — Shared rate limits and backpressure

**Goal:** bound abuse, queue growth, and expensive concurrency across replicas.

**Edit:** `backend/security/rate_limiter.py`, `backend/config/settings.py`,
`backend/api/main.py`, `backend/api/routers/contact.py`,
`backend/api/routers/integrations.py`, `backend/api/routers/workflows.py`,
`terraform/modules/cloud_run/main.tf`, `frontend/src/lib/api.ts`.
**Add:** `backend/repositories/rate_limit_repository.py`,
`backend/tests/test_distributed_limits.py`.

1. Choose a shared atomic counter implementation based on measured traffic and
   operational cost; do not add infrastructure merely to name a technology.
2. Apply endpoint, user, tenant, and unauthenticated-client limits with expiry.
   Configure trusted proxies at ingress; raw client headers are not identities.
3. Separate request rate, active-run concurrency, browser concurrency, and queue
   capacity. Return a stable overload response with retry guidance.
4. Add fair scheduling so one organization cannot occupy every worker slot.
5. Surface retry timing and queue status without automatically duplicating work.

**Acceptance:** multiple replicas share one allowance; spoofed forwarding headers
do not bypass limits; memory/cardinality is bounded; datastore failure follows a
documented policy. Load-test overload and recovery, not only normal throughput.
**Rollout:** observe then enforce; edge protection supplements authenticated
application quotas. Document limits in `docs/api-overview.md` and the runbook.

## Phase 04 — Durable commands, events, and build recovery

**Goal:** both user-created and agent-created work resume through one lifecycle.

**Edit:** `backend/services/connector_build_service.py`,
`backend/agents/orchestrator/orchestrator_agent.py`, `backend/engine/engine.py`,
`backend/api/routers/integrations.py`, `backend/repositories/base.py`,
`backend/repositories/sqlite/workflow_repository.py`,
`backend/repositories/firestore/workflow_repository.py`,
`backend/repositories/pubsub_message_bus.py`, `backend/worker.py`,
`frontend/src/app/dashboard/integrations/create/page.tsx`.
**Add:** `backend/repositories/outbox_repository.py`,
`backend/services/command_service.py`, `backend/tests/test_command_recovery.py`.

1. Persist command ID, deduplication key, owner, parent run/task, requested
   capability, state/version, and cancellation intent.
2. Persist work and its publish intent atomically; a dispatcher retries the
   outbox. Consumers deduplicate repeated delivery rather than assuming one send.
3. Replace tracked inline integration builds with the same queued build command
   used by the API. Resume the parent through a durable, deduplicated transition.
4. Fence every task completion with attempt/lease ownership. Heartbeats cannot
   restore a lease already acquired by a newer worker.
5. Distinguish cancellation request, cancellation acknowledgement, and effects
   already committed. Keep a stable build/run URL across reconnects.

**Acceptance:** crash between every persistence/publish/claim/complete boundary;
redeliver events; cancel during build; restart during authentication. No lost
accepted command, duplicate logical completion, or stale completion overwrite.
**Rollout:** additive schemas and backward-compatible consumers before producers;
drain old work before retiring formats. Update architecture and event diagrams.

## Phase 05 — External effects and uncertain outcomes

**Goal:** retries cannot silently duplicate emails, records, or transactions.

**Edit:** `backend/engine/idempotency_guard.py`,
`backend/repositories/sqlite/idempotency_repository.py`,
`backend/repositories/firestore/idempotency_repository.py`,
`backend/mcp/tool_router.py`, `backend/services/email_service.py`,
`backend/services/action_approval.py`, `backend/services/approvals_engine.py`.
**Add:** `backend/services/reconciliation_service.py`,
`backend/api/routers/reconciliation.py`,
`frontend/src/components/UncertainOutcomePanel.tsx`.

1. Bind effect IDs to canonical arguments, target, actor, tool revision, and
   credential context. Use provider idempotency keys where supported.
2. Store provider receipts and explicit uncertain states. Never infer failure
   merely because the response or local success commit was lost.
3. Implement provider-specific read-back reconciliation. For providers without
   sufficient evidence, require a documented operator resolution.
4. Give resolution endpoints current authorization, compare-and-set protection,
   audit records, and a reason/receipt. A blind status toggle is insufficient.
5. Revalidate approval when target or payload changes during repair. Treat a
   compensating action as a new authorized effect, not a guaranteed rollback.

**Acceptance:** failure after remote success, timeouts, duplicate events, operator
races, and orphaned locks cannot cause automatic repeated writes. Expand
`test_uncertain_reconciliation.py` with controlled provider fixtures.
**Rollout:** start with one provider and document its guarantees; no global
exactly-once claim. UI offers inspect/reconcile, not an ambiguous Retry button.

## Phase 06 — Connector validation, provenance, and revision activation

**Goal:** only the exact tested operation and credential context can execute.

**Edit:** `backend/services/tool_validation.py`,
`backend/services/connector_revisions.py`, `backend/mcp/tool_policy.py`,
`backend/mcp/tool_router.py`, `backend/agents/mcp_factory/mcp_factory_agent.py`,
`backend/api/routers/integrations.py`, `backend/models/mcp_schemas.py`,
`frontend/src/app/dashboard/integrations/[mcp_id]/page.tsx`,
`frontend/src/app/dashboard/integrations/page.tsx`.
**Add:** `backend/services/connector_validation_service.py`,
`frontend/src/components/ConnectorCoverageTable.tsx`.

1. Remove duplicate coverage logic. Calculate execution eligibility and displayed
   coverage from the same definition digest and current credential version.
2. Preserve official/extracted/inferred provenance; missing evidence is unknown,
   not automatically official. Display selected and omitted operations.
3. Implement schema, authentication, parameter encoding, pagination, empty-result,
   timeout, rate-limit, and response-schema checks. A GET is not intrinsically safe.
4. Test writes only in controlled fixtures/accounts with the appropriate approval.
   Keep unsupported operations visibly unavailable.
5. Persist draft, authenticated, validated, approved, active, and retired lifecycle
   transitions. Pin running tasks to immutable revisions; rollback activates a
   validated revision without mutating historical records.

**Acceptance:** changing schema, destination, credential version, or risk invalidates
the right evidence; stale probes never inflate coverage. Extend
`test_connector_coverage.py`, `test_connector_revisions.py`, and self-extension tests.
**Rollout:** invalidate legacy evidence conservatively and guide reconnection.
Update `docs/mcp-builder.md`, capability descriptions, and API examples.

## Phase 07 — Standalone MCP and generated-code isolation

**Goal:** provide a real export/execution product only when its runner is proven.

**Edit:** `backend/mcp/standalone_packaging.py`,
`backend/agents/mcp_factory/code_generator.py`,
`backend/agents/mcp_factory/sandbox_tester.py`,
`backend/mcp/sandbox/sandbox_controller.py`, `requirements.txt`.
**Add:** `backend/services/isolated_runner.py`, `runners/mcp/Dockerfile`,
`backend/tests/test_runner_isolation.py`.

1. Separate immutable source/dependency/runtime contents from mutable test records.
   Hash executable contents canonically; deployment rechecks that digest.
2. Resolve exact dependency versions and integrity metadata. Ranges labeled a
   lock are not reproducible builds. Produce a software bill of materials.
3. Select a supported sandbox runtime for untrusted code with no ambient secrets,
   restricted filesystem, bounded CPU/memory/processes/time, and enforced egress.
   Container packaging alone is not evidence of adequate hostile-code isolation.
4. Run actual initialize, tools/list, tools/call, cancellation, and shutdown tests.
   Only the trusted runner can issue evidence tied to an artifact digest.
5. Expose adapter versus standalone-server status and actual deployment support.

**Acceptance:** post-test content tampering blocks deployment; seeded host secrets
are inaccessible; denied egress and filesystem writes fail; descendants terminate.
Extend `test_standalone_packaging.py` beyond manually setting passing booleans.
**Rollout:** keep execution disabled until the selected runner passes. Ship
declarative adapters independently when their required gates are complete.

## Phase 08 — Browser ownership, isolation, and human handoff

**Goal:** a browser is an owned, bounded execution resource with defined recovery.

**Edit:** `backend/services/web_agent.py`, `backend/services/browser_sessions.py`,
`backend/services/auth_challenges.py`,
`backend/agents/orchestrator/orchestrator_agent.py`,
`backend/services/action_approval.py`, `backend/worker.py`.
**Add:** `backend/services/browser_runtime.py`,
`backend/repositories/browser_session_repository.py`,
`frontend/src/components/BrowserSessionPanel.tsx`.

1. Fix resource cleanup for normal, failed, cancelled, and paused runs; close owned
   Playwright objects even when no registry entry was created.
2. Persist session metadata, owner, worker lease, allowed origins, expiry, and
   checkpoint location. Live browser objects stay in their owning worker.
3. Add capacity limits, idle expiry, heartbeat, and cleanup after worker loss.
   Reconstruct only supported encrypted session state; reauthenticate when needed.
4. Enforce action-level authorization before consequential clicks/submissions.
   Bind secret injection to the intended origin and field; guard all egress,
   not just top-level navigation. Review redirects, popups, and WebSockets.
5. Build authenticated human takeover with short-lived access, clear ownership,
   redaction, and resume verification. CAPTCHA/OTP/MFA remain human actions.

**Acceptance:** no browser process leaks after success/failure/cancellation; two
tenants cannot observe each other's sessions; worker loss produces a resumable
or explicit reauthentication state. Test takeover expiration and revocation.
**Rollout:** use test accounts and a small supported-site set; document unsupported
session recovery. Browser state and screenshots follow retention controls.

## Phase 09 — Advanced DOM targeting and browser actions

**Goal:** handle dynamic applications without clicking stale or ambiguous targets.

**Edit:** `backend/services/web_agent.py`, `backend/mcp/website_mcp.py`.
**Add:** `backend/services/dom_observer.py`,
`backend/services/browser_action_validator.py`,
`backend/tests/test_dom_targeting.py`, `frontend/e2e/browser-supervision.spec.ts`.

1. Observe semantic role, accessible name, label, visibility, enabled state, frame,
   origin, and a snapshot version. Prefer semantic locators over positional IDs.
2. Re-resolve targets against the current page before acting. Ambiguous matches
   trigger another observation or clarification; do not silently choose the first.
3. Support frame locators, open shadow roots, virtualized lists, dialogs, tabs,
   popups, and redirects with explicit ownership. Mark unsupported closed roots.
4. Use meaningful postconditions and locator waits instead of broad network-idle
   assumptions. Detect no-progress loops and repeated failed actions.
5. Add bounded uploads/downloads with type/size checks and artifact ownership.
   Use visual targeting as a controlled fallback with target verification.
6. Show the proposed target, action, resulting change, and failure explanation in
   the supervision UI without exposing credentials or unrelated page content.

**Acceptance:** fixtures reorder elements, delay rendering, change frames, hide
controls, and inject hostile text. The agent either completes the intended action
or stops clearly; it never reports success from a click alone.
**Rollout:** publish a capability matrix by tested browser/site pattern. See the
official Playwright locator documentation in the references below.

## Phase 10 — Verified ReAct orchestration

**Goal:** each reason-and-act cycle is bounded, authorized, and outcome-checked.

**Edit:** `backend/agents/orchestrator/orchestrator_agent.py`,
`backend/agents/planner/planner_agent.py`, `backend/engine/engine.py`,
`backend/models/schemas.py`, `backend/mcp/tool_router.py`,
`frontend/src/components/ExecutionTimeline.tsx`,
`frontend/src/components/TaskDetailsPanel.tsx`.
**Add:** `backend/models/execution_step.py`,
`backend/services/outcome_verifier.py`, `backend/tests/test_react_execution.py`.

1. Define structured observation, action, policy decision, result, and verification
   records with IDs, timestamps, revisions, and evidence references.
2. Keep untrusted page/tool content separate from policy and user instructions.
   Validate proposed actions deterministically before execution.
3. Cap iterations, wall time, spend, tool calls, and repeated equivalent actions.
4. Require observable task postconditions. Model-declared completion can request
   verification but cannot alone establish external success.
5. Checkpoint at meaningful boundaries; replay observations safely and never
   automatically repeat external effects during replay.
6. Show concise decision summaries and evidence, not private internal reasoning.

**Acceptance:** misleading success text, unavailable tools, stale plans, injection,
and infinite-loop fixtures produce correct completion or bounded failure.
**Rollout:** begin with read-only tasks, then explicitly approved writes. Extend
the existing DAG/orchestrator instead of installing a second competing engine.

## Phase 11 — Advanced self-healing

**Goal:** repair the current run without changing its goal or exceeding authority.

**Edit:** `backend/agents/recovery/recovery_agent.py`,
`backend/models/recovery.py`, `backend/engine/plan_repair.py`,
`backend/engine/engine.py`, `backend/services/operations_summary.py`.
**Add:** `backend/services/recovery_policy.py`,
`backend/repositories/recovery_episode_repository.py`,
`frontend/src/components/RecoveryPanel.tsx`.

1. Classify transient transport, quota, schema, missing input, credentials,
   authorization, changed DOM, provider ambiguity, and infrastructure failures.
2. Map each class to permitted recovery actions. Retry transient reads; repair
   schema-constrained inputs; reconnect credentials; reconcile uncertain writes.
3. Store original goal, failure evidence, proposed repair, policy decision,
   attempt count, reserved budget, and outcome in a recovery episode.
4. Verify the repaired action's postcondition independently. Detect cycling
   between equivalent repairs and stop with an actionable incident.
5. Require fresh approval for changed effect targets/payloads. Never let repair
   increase its own tool permissions or suppress audit events.

**Acceptance:** fault injection proves bounded attempts, no duplicate side effects,
and correct escalation. Measure recovery rate by failure class, time to recovery,
false-success rate, and additional cost. Compare with a non-repair baseline.
**Rollout:** enable one failure class at a time behind policy controls. The UI
shows evidence and remaining attempts, not an unqualified “self-healed” label.

## Phase 12 — Evaluation data and outcome verification

**Goal:** replace completion proxies and placeholder metrics with trustworthy data.

**Edit:** `backend/services/evaluation_service.py`,
`backend/models/evaluation.py`, `backend/services/run_analytics.py`,
`backend/engine/engine.py`, `backend/api/routers/usage.py`.
**Add:** `backend/repositories/evaluation_repository.py`,
`backend/services/evaluation_runner.py`, `evals/README.md`,
`frontend/src/app/dashboard/evaluations/page.tsx`.

1. Define task-specific success, factuality, completeness, authorization, latency,
   and cost measures. Use provider receipts and deterministic assertions first.
2. Wire actual tool/model events into persistent records; remove placeholder zero
   tokens, guessed model attribution, and unmeasured tool counts.
3. Version datasets, evaluator code, prompts, models, connectors, and fixtures.
   Separate training/development, held-out evaluation, and adversarial cases.
4. Include failure cases, partial completion, user corrections, and unsupported
   tasks. A model judge can supplement evidence with stated limitations.
5. Redact and govern captured data; avoid cross-tenant learning without permission.

**Acceptance:** evaluation records survive restart and reproduce results for fixed
fixtures; scorer changes cannot rewrite historical scores silently. Independent
expected outcomes catch deliberately incorrect “successful” runs.
**Rollout:** observe without changing routing first. Display sample sizes,
uncertainty, missing measurements, and evaluator version in the dashboard.

## Phase 13 — Controlled self-improvement loop

**Goal:** improve future behavior through evaluated, reversible changes.

**Edit:** `backend/services/evaluation_service.py`,
`backend/services/plugin_registry_service.py`, `backend/services/connector_revisions.py`,
`backend/agents/agent_factory.py`.
**Add:** `backend/services/improvement_service.py`,
`backend/repositories/experiment_repository.py`,
`backend/api/routers/experiments.py`,
`frontend/src/app/dashboard/experiments/page.tsx`.

1. Turn reviewed failure clusters into candidate prompt, routing, connector, or
   implementation changes with a stated hypothesis and expected benefit.
2. Keep baseline and candidate immutable; evaluate both on the same held-out set.
3. Run promising candidates in shadow mode, then a bounded canary cohort with
   predeclared success, regression, cost, and rollback thresholds.
4. Separate proposal, evaluation, approval, activation, and rollback permissions.
   Agents cannot modify their own authorization or grading rules.
5. Produce reviewable diffs and evidence. Code proposals use ordinary review and
   release procedures; prompts and policies also have versioned rollback.

**Acceptance:** a candidate that improves average quality but violates a critical
constraint cannot promote; rollback restores the prior policy; every promotion
links its dataset, results, actor, and deployment version.
**Rollout:** start with prompts for read-only tasks. Self-healing handles one run;
self-improvement changes future runs and therefore has a separate release gate.

## Phase 14 — Contextual routing and advanced reinforcement learning

**Goal:** improve model/tool selection using verified outcomes and bounded exploration.

**Edit:** `backend/services/adaptive_policy.py`,
`backend/repositories/sqlite/adaptive_policy_repository.py`,
`backend/repositories/firestore/sidecar.py`, `backend/engine/engine.py`,
`backend/api/routers/usage.py`, `frontend/src/app/dashboard/operations/page.tsx`.
**Add:** `backend/models/routing_decision.py`,
`backend/services/offline_policy_evaluation.py`,
`backend/tests/test_routing_policy.py`.

1. Fix reward attribution: a model call finishing without exception is not proof
   of task success. Join delayed verified outcomes to the actual selected model.
2. Add context such as task class, capability, input size, budget, and latency need.
   Filter candidates by permissions, provider configuration, and requirements.
3. Log context, eligible actions, selected action, selection probability, reward
   components, policy version, and missing/late feedback.
4. Compare contextual-bandit candidates offline with adequate action support and
   uncertainty estimates. Poor logging cannot be repaired by a clever estimator.
5. Explore only within allowed choices and a bounded budget; monitor drift and
   reward manipulation. Keep security constraints outside the scalar reward.
6. Consider model fine-tuning or multi-step RL only after sufficient consented,
   representative data and a safe evaluation environment exist.

**Acceptance:** compare with fixed routing using held-out tasks and controlled
canaries; report verified quality, cost, latency, and confidence by task category.
**Rollout:** the current UCB/epsilon router is a foundation, not weight training.
Model training and external-provider policy restrictions require separate design.

## Phase 15 — Specialized subagents and task coordination

**Goal:** add specialization where it improves measured outcomes without duplicating work.

**Edit:** `backend/agents/agent_factory.py`, `backend/models/plugin.py`,
`backend/services/plugin_registry_service.py`, `backend/api/routers/agents.py`,
`frontend/src/app/dashboard/agents/page.tsx`.
**Add:** `backend/agents/verifier/verifier_agent.py`,
`backend/agents/connector_validator/validator_agent.py`,
`backend/services/agent_task_contracts.py`.

| Agent/service | Assignment | Boundary |
|---|---|---|
| Outcome Verifier | Check completion and deliverables | Read-only evidence access |
| Recovery Analyst | Propose repairs | No permission escalation |
| Connector Validator | Generate and execute permitted tests | Controlled accounts |
| Browser Navigator | Execute authorized browser actions | Scoped session/origins |
| DOM Resolver | Refresh observations and identify targets | No independent effects |
| Reconciliation Agent | Compare receipts with uncertain executions | No blind replay |
| Evaluation Curator | Maintain reviewed regression cases | Redacted, permitted data |
| Improvement Proposer | Draft versioned changes | Cannot self-promote |
| Release Verifier | Assemble candidate evidence | Cannot self-certify |

Cost Governor and authorization checks should remain deterministic services;
they may explain decisions through an agent but must not depend on model consent.
Give every delegated task a parent, typed inputs/outputs, scope, budget, deadline,
lease, cancellation, and result schema. Bound fan-out and prevent delegation loops.

**Acceptance:** agent failures cannot leak data or strand parents; duplicate
assignments deduplicate; total spend and concurrent children remain bounded.
**Rollout:** introduce verifier and connector validator first; add others only
when evaluation beats the simpler single-agent implementation.

## Phase 16 — Memory, retrieval, and knowledge governance

**Goal:** retrieve useful, current evidence without crossing ownership boundaries.

**Edit:** `backend/api/routers/memory.py`, `backend/models/memory.py`,
`backend/services/memory_search.py`, `backend/services/embedding_service.py`,
`backend/repositories/sqlite/memory_repository.py`,
`backend/repositories/firestore/memory_repository.py`.
**Add:** `backend/services/memory_lifecycle.py`,
`frontend/src/app/dashboard/memory/page.tsx`.

1. Separate temporary run context, episodic outcomes, reusable procedures, and
   user-approved preferences. Persist provenance, owner, version, and expiry.
2. Apply authorization before retrieval and again before disclosure; a vector
   match is never permission to read the corresponding content.
3. Add deduplication, conflicting-fact handling, source freshness, and explicit
   user corrections. Display sources and why an item was retrieved.
4. Support inspect, edit, export, forget, and retention controls; propagate
   deletion to embeddings, indexes, caches, and derived training datasets.
5. Evaluate hybrid retrieval/reranking on actual tasks before adopting it.

**Acceptance:** adversarial similar queries cannot retrieve another tenant's data;
deletion is effective; stale or injected memory cannot override policy. Measure
retrieval usefulness and false associations on held-out queries.
**Rollout:** private memory first; organization sharing is explicit and audited.

## Phase 17 — Deliverables, provenance, and document quality

**Goal:** completion means an authorized artifact exists and meets the user's request.

**Edit:** `backend/services/artifact_builder.py`,
`backend/api/routers/artifacts.py`, `backend/services/resume_builder.py`,
`backend/services/resume_renderer.py`, `backend/services/resume_tailor_service.py`,
`backend/services/ats_analyzer_service.py`,
`frontend/src/app/dashboard/resume/page.tsx`,
`frontend/src/app/dashboard/studio/page.tsx`.
**Add:** `backend/services/artifact_verifier.py`,
`frontend/src/components/ArtifactEvidencePanel.tsx`.

1. Persist artifact type, owner, manifest, checksum, source references, and creation
   revision. Validate structure and authorized download access.
2. Track source-to-field provenance for resumes and other factual documents;
   flag unsupported dates, employers, degrees, and numerical claims.
3. Validate rendered outputs, not just JSON/text generation. Add export formats
   only when they open and render correctly in the supported viewers.
4. Show a change comparison, unsupported claims, and missing inputs before final
   acceptance. Label ATS scores as product heuristics with a stated rubric.
5. Make generated applications pass their declared build/test scope before
   labeling them runnable. Keep unexecuted source explicitly labeled.

**Acceptance:** wrong-user downloads fail; corrupted artifacts fail verification;
adversarial resume cases cannot invent facts unnoticed. Extend
`test_artifact_integrity.py` and `test_resume_deliverable.py` with rendered fixtures.
**Rollout:** support a small set of high-quality deliverables before adding formats.

## Phase 18 — React architecture, sessions, and resilient interaction

**Goal:** keep user work intact during navigation, reconnects, and partial failure.

**Edit:** `frontend/src/lib/api.ts`, `frontend/src/lib/auth-session.ts`,
`frontend/src/lib/auth-context.tsx`, `frontend/src/components/AppProviders.tsx`,
`frontend/src/app/dashboard/layout.tsx`,
`frontend/src/app/dashboard/workspace/[run_id]/page.tsx`,
`frontend/src/app/dashboard/integrations/create/page.tsx`,
`frontend/src/hooks/useNetworkStatus.ts`.
**Add:** `frontend/src/hooks/useRunEvents.ts`,
`frontend/src/lib/run-state.ts`, `frontend/src/components/RunStatusBanner.tsx`.

1. Define shared UI state contracts for queued, running, approval, credentials,
   recovery, uncertainty, completion, failure, and cancellation. Derive them from
   backend state rather than independent page-specific guesses.
2. Consolidate stream reconnect/deduplication and request cancellation. Reconcile
   missed events with persisted snapshots after reconnect.
3. Preserve drafts and selected work across refresh/login expiry. Test two tabs
   sharing a single-use refresh token and browsers without Web Locks.
4. Use explicit error/loading/empty states and mutation conflict handling. Optimism
   is suitable for reversible edits, not proof of successful external effects.
5. Profile long lists and DAGs; virtualize where measurements justify it. Keep
   heavy optional views dynamically loaded and server-render static surfaces.
6. Evaluate a secure HttpOnly-cookie/BFF session migration with CSRF, CORS, logout,
   expiry, and API-client requirements before replacing current token handling.

**Acceptance:** real-backend Playwright journeys preserve work through disconnect,
expired sessions, navigation, duplicate events, and refresh. Extend existing E2E
specs with concurrent tabs; mocked API tests remain a separate suite.
**Rollout:** incremental page migration; read `frontend/AGENTS.md` and local Next
documentation before implementation. Update `frontend/README.md` with contracts.

## Phase 19 — Design system, accessibility, and useful 3D

**Goal:** make the workspace clearer and visually distinctive without reducing usability.

**Edit:** `frontend/src/app/page.tsx`, `frontend/src/app/drafting.css`,
`frontend/src/app/globals.css`, `frontend/src/components/CyanotypeScene.tsx`,
`frontend/src/components/TiltCard.tsx`, `frontend/src/components/GlassPanel.tsx`,
`frontend/src/components/WorkflowGraph.tsx`, `frontend/src/components/DAGNode.tsx`,
`frontend/src/components/ThemeSwitch.tsx`.
**Add:** `frontend/src/lib/motion-policy.ts`,
`frontend/src/components/WorkflowScene.tsx`,
`frontend/e2e/accessibility.spec.ts`, `frontend/e2e/scene-fallback.spec.ts`.

1. Standardize typography, spacing, elevation, focus, status colors, form feedback,
   dialogs, and responsive layouts. Preserve the existing visual language.
2. Improve the current globe/thread scene's lighting and composition rather than
   adding unrelated effects. Distinguish illustration from live telemetry.
3. Add optional 3D workflow/agent relationships with a readable 2D equivalent.
   Tables, forms, and approval details remain directly accessible.
4. Add adaptive quality, bounded pixel density, offscreen/hidden suspension,
   reusable geometry, and fewer allocations per frame. Render on demand when idle.
5. Apply reduced-motion policy to tilt, transitions, and canvas motion consistently.
   Preserve normal mobile scrolling; do not add scene zoom gestures on mobile.
6. Provide keyboard equivalents, textual state, contrast, and WebGL fallback.

**Acceptance:** agree on representative devices and measurable budgets before
implementation; suggested targets are responsive input, 60 fps desktop/30 fps
low-tier scene interaction where feasible, and a usable static fallback. Record
measurements rather than claiming universal frame rates. Perform manual keyboard
and assistive-technology checks alongside automated accessibility tests.
**Rollout:** 3D is optional and can be disabled independently from workflow work.

## Phase 20 — Observability, CI, deployment, and recovery assurance

**Goal:** prove the supported release on the environment that will actually run it.

**Edit:** `cloudbuild.yaml`, `cloudbuild.frontend.yaml`, `Dockerfile`,
`Dockerfile.worker`, `frontend/Dockerfile.frontend`, `scripts/verify-local.sh`,
`scripts/verify_deployed_workflows.py`, `terraform/modules/pubsub/main.tf`,
`terraform/modules/cloud_run/main.tf`, `terraform/modules/iam/main.tf`,
`backend/services/operations_summary.py`, `docs/ops-runbook.md`.
**Add:** `docs/evidence/release-template.md`,
`scripts/verify_release_evidence.py`, `tests/load/README.md`.

1. Correlate tenant-safe run/task/command/build/effect IDs across API, workers,
   tools, and provider calls. Record queue age, stuck leases, latency, cost,
   recovery, and outcome-verification rates with redacted logs.
2. Define service objectives and alert owners. Test alert delivery and operator
   actions; a dashboard without response procedures is incomplete.
3. Test the actual candidate API/worker images and production frontend build.
   Add real-backend browser gates, dependency/image/secret scanning, and evidence
   manifests. Fail release on required missing evidence, not just test failures.
4. Exercise worker kill/reclaim, Pub/Sub redelivery/DLQ replay, Firestore failure,
   provider timeout, load/soak, backup restore, key rotation, and rollback.
5. Use expand/migrate/contract changes with old/new version compatibility tests.
   Roll back application and policy versions together when required.
6. Verify actual resource types: a Cloud Run Worker Pool is not an HTTP service.
   Configure least-privilege identities and a tested stop procedure for each.

**Acceptance:** all launch-scope gates have commit/image/environment evidence,
restored data is usable, rollback succeeds, and incident responders receive alerts.
**Rollout:** staging, limited cohort, measured expansion. Formal certification
requires a named standard and its assessment; this roadmap grants none.

## Phase 21 — Product expansion and enterprise options

**Goal:** add differentiated capabilities after the execution contract is dependable.

**Edit:** `backend/api/routers/schedules.py`, `backend/api/routers/agents.py`,
`backend/api/routers/orgs.py`, `backend/api/routers/integrations.py`,
`backend/services/plugin_registry_service.py`,
`frontend/src/app/dashboard/catalog/page.tsx`,
`frontend/src/app/dashboard/schedules/page.tsx`,
`frontend/src/app/dashboard/settings/page.tsx`.
**Add, only when selected:** `backend/services/workflow_templates.py`,
`backend/services/dry_run_service.py`,
`frontend/src/components/CommandPalette.tsx`,
`frontend/src/app/dashboard/templates/page.tsx`.

| Candidate feature | Backend work | Frontend work | Gate before release |
|---|---|---|---|
| Workflow templates | Versioned schemas, required capabilities, migration | Preview, configure, compare versions | Template cannot expand permissions silently |
| Dry run and cost estimate | Side-effect-free planner and bounded estimates | Show proposed effects and uncertainty | No provider writes in dry mode |
| Run comparison/replay | Immutable event/evidence references | Compare decisions and outputs | Replay cannot repeat effects |
| Connector marketplace | Reviewed revisions, provenance, revocation | Trust/coverage cards and update diffs | Publisher secrets never transfer |
| Schedule calendar | Time-zone/DST rules, deduplication, missed-run policy | Calendar, pause, next-run explanation | DST and duplicate-trigger tests |
| Personal automation inbox | Attention queries and notification delivery | Approvals, failures, artifacts in one view | Tenant isolation and actionable status |
| Account MFA/passkeys | Enrollment, recovery, revocation | Recovery-aware settings | Lockout and recovery tests |
| Enterprise SSO/provisioning | Identity mapping and role lifecycle | Organization policy and access review | Deprovisioning takes effect everywhere |
| Data governance | Retention, export/delete, regional requirements | Consent and retention controls | Verified deletion/restoration behavior |

**Acceptance:** each selected feature receives a separate scope, owner, tests,
operating cost, and release gate. SSO, regional controls, billing, and model
training are optional product decisions, not automatic prerequisites for every
deployment. Update public capability claims only after acceptance.

## Proposed shared contracts

These records should be schema-versioned and tenant-scoped. Use repositories for
both supported stores; do not introduce a SQLite-only safety guarantee.

| Record | Essential fields | Invariant |
|---|---|---|
| Command | ID, dedup key, owner, type, payload digest, state/version | Repeated request does not create new logical work |
| Budget reservation | ID, user/tenant, run/task, provider, estimate, state, expiry | Available capacity is consumed atomically |
| Execution evidence | action/result IDs, revision, receipt, verifier, outcome | Completion references observable evidence |
| Connector validation | content digest, operation, credential version, test context | Evidence applies only to the tested capability |
| Browser session | owner, worker lease, scope, expiry, checkpoint reference | One current owner; sensitive state expires |
| Recovery episode | failure class, attempts, repair, budget, result | Repair stays within original authority |
| Experiment | baseline/candidate, dataset, metrics, approval, rollout | Promotion is reversible and attributable |
| Routing decision | context, candidates, selection probability, outcome | Offline evaluation can identify logging policy |

Avoid storing raw secrets, full sensitive page captures, or unrestricted model
reasoning in these records. Store only necessary evidence with access controls.

## Documentation ownership and completion checklist

| Document | Maintains |
|---|---|
| `README.md` | Supported scope, quick start, high-level limits |
| `docs/full-stack-roadmap.md` | Planned phases, dependencies, file map, acceptance |
| `docs/PHASES.md` | Navigation and translation from the earlier A–G roadmap |
| `docs/implementation-status.md` | Current implementation and unverified changes |
| `docs/RELEASE_EVIDENCE.md` | Dated test/operational evidence and reopened findings |
| `docs/production-readiness.md` | Launch gates and release verdict |
| `frontend/README.md` | Frontend setup, architecture, validation, design boundaries |
| `docs/SECURITY.md` | Enforced controls versus remaining boundaries |
| `docs/architecture.md` | Actual execution and storage topology |
| `docs/mcp-builder.md` | Connector products, lifecycle, evidence, limitations |
| `docs/ops-runbook.md` | Tested incident, recovery, and rollout procedures |

Before closing a phase: verify both stores where applicable; test adversarial and
failure cases; review migration/rollback; verify visible UI behavior; record
evidence; update user-facing capability wording; identify remaining exclusions.
Do not turn unchecked plan items into “implemented” statements during doc edits.

## Primary technical references

Consult installed-version documentation before implementation; online docs may
describe a newer release than this repository. These sources explain primitives,
not proof that AgentOS implements the proposed architecture.

- [React lazy loading](https://react.dev/reference/react/lazy): defer optional component code.
- [Playwright locators](https://playwright.dev/docs/locators): semantic targeting and locator retry behavior.
- [React Three Fiber performance](https://r3f.docs.pmnd.rs/advanced/scaling-performance): on-demand rendering and resource efficiency.
- [Vowpal Wabbit offline policy evaluation](https://vowpalwabbit.org/docs/vowpal_wabbit/python/latest/tutorials/off_policy_evaluation.html): evaluate candidate contextual-bandit policies from logged data.
- [ReAct research](https://react-lm.github.io/): reasoning/action/observation interaction as a starting point, not a production safety boundary.
