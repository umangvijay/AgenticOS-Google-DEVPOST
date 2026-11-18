# Release Evidence Packet — Sandtable (AgentOS)

Audit baseline: *AgentOS / Sandtable — Full-stack audit & advancement plan v4.0* (30 Oct 2026).
This packet tracks the audit's required evidence, item by item, with the honest
status of each: **verified (local)** means implemented with regression tests that
run in this repository; **pending** means it requires resources this repository
cannot produce (a real deployment, provider accounts, or independent review).

A finding is *closed* only when its evidence meets the acceptance criteria.
A code change or passing narrow test alone does not close a broader guarantee.

## Phase 3 admission & capacity — 31 October 2026

Status: **verified (local)** for every item below; deployed-replica evidence
remains **pending** (needs a real multi-instance deployment drill, recorded
here after it runs). Spec: docs/admission-and-capacity.md §3.0–3.23.

| Item | Evidence (this repository) |
|---|---|
| Cleanup never deletes an active window | `test_distributed_rate_limits.py::test_cleanup_during_active_exhausted_window_keeps_enforcing`, `::test_cleanup_with_any_cutoff_never_deletes_active_windows` |
| Zero-limit first request refused, no record | `::test_first_request_with_zero_allowance_rejected_without_record` |
| Cross-replica allowance | `::test_independent_connections_share_one_allowance`, `::test_independent_processes_share_one_allowance` (two OS processes via subprocess) |
| Typed decisions & outage modes | `test_admission_policy.py` (4 enforcement modes, 503 fail-closed, degraded fail-open) |
| Policy inventory totality | `test_admission_policy.py::test_every_protected_group_resolves_to_a_policy`, `::test_unknown_group_is_a_configuration_error` |
| Capacity leases (atomic/idempotent/fenced/TTL) | `test_capacity_leases.py` (15 tests incl. racing acquisition and generation fencing) |
| Queue backpressure & bounded bus | `test_queue_backpressure.py` (tenant caps, duplicates, expiry; full-queue retry deadlock scenario; DLQ bounds; shutdown) |
| Atomic schedule claims | `test_schedule_admission.py` (duplicate claims, release-on-crash, two connections racing) |
| Provider backoff | `test_provider_backoff.py` (bounded growth, Retry-After, jitter desynchronization) |
| Phase 4 command lifecycle | test_command_acceptance.py (13): durable acceptance (all artifacts committed), idempotency contract, conditional transitions, cancellation races, outbox crash-recovery, parent waits, reconciler |
| Phase 5 effects (§5.2–5.12/§5.20) | test_effect_dispatch.py (10, service-boundary): identity preservation, partial SMTP acceptance preserved, disconnect to UNKNOWN with no resend, duplicate submission never re-sends, empty-search is not proof, resolution races (1 winner/1 conflict), operator-guess rule |
| Phase 6 connectors (§6.4–6.15/§6.21–6.22) | test_connector_phase6.py (22): provenance stays inferred through probes, identity invariant to order/probes, coverage context rule, rotation supersedes validation, activation races (1 winner/1 conflict), restore strips evidence, migration idempotent |
| Phase 7 standalone contract (§7.7/§7.19/§7.20/§7.10) | test_standalone_phase7.py (14): attestation forgery/cross-artifact rejection, deployment matrix (content-change block, isolation bypass, revocation, foreign caller), write-profile gating, artifact identity invariance |
| §7.14/§7.15 broker | test_runner_security.py: capability replay rejected across execution/tenant/artifact; destination/method/expiry/budget enforced at the boundary |
| §7.9 dependency lock | hash required for every package; changed package fails verification; extra deps rejected |
| §7.16 protocol suite | skipped-required ≠ passed; uncertain ≠ pass; failed init = no protocol-pass claim |
| §7.22 repair | credential failures do not regenerate; proposer bounded by attempt/cost limits |
| §7.11/§7.24 lifecycle | orphaned executions reclaimed; cleanup state separate; kill switches block dispatch |
| Phase 8 browser runtime (§8.4–8.19/§8.24) | test_browser_phase8.py (17): ownership races (1 winner), epoch fencing, transition matrix, idle-vs-absolute timers, network policy categories, injection purpose/origin/challenge checks, material digest binding, effect restart-reconcile, grant lifecycle, honest recovery levels |
| Phase 9 targeting (§9.2–9.8/§9.17/§9.21) | test_dom_targeting.py (23) + test_browser_session_concurrency.py (4): semantic resolution with ambiguity reporting, staleness per dimension, pipeline enforcement, outcome levels, progress tracking, 4-worker concurrent races, stable business-action identity across ownership changes |
| Phase 10 ReAct (§10.1–10.6) | test_react_execution.py (15): step digest binding, untrusted-content separation, all five bound dimensions, outcome-gated completion (uncertain effects, bounds exceeded, model-done-requires-verification), misleading-success rejection |
| §10.1 workflow dedup | test_execution_steps.py: dedup returns original run, digest conflict, Firestore contract error |
| §10.5/§10.6 steps | test_execution_steps.py: lifecycle happy path, DISPATCHING→OUTCOME_UNCERTAIN, CAS cancellation race |
| §10.8 verifier | test_react_execution.py: no-criteria needs_review, nonexistent artifact fails, artifact without resolver needs_review; test_dom_targeting.py: click ≠ verified |
| §10.2 browser | WORKER_ID import fixed; cleanup ordering fixed |
| §10.7 criteria contract | test_react_scenarios.py: write-task non-empty rejected, effect_resolved accepted, read-task content criteria sufficient |
| §10.22 E2E scenarios | test_react_scenarios.py (13): misleading success, fabricated artifacts, uncertain effects, prompt injection, loop limits, budget exhaustion, duplicate delivery, crash recovery, cancellation races |
| Phase 11 recovery episodes (§11.2/§11.3/§11.7/§11.8/§11.9) | test_recovery_episodes.py (9): state machine, atomic claims (concurrent → 1 winner), generation advancement, abandoned episodes, persistent counters |
| Phase 11 worker fixes (§11.0 Findings 7–11) | test_recovery_worker_acceptance.py (5): duplicate delivery, strict action validation, cycle detection, repair-queued event, misleading-success block |
| Phase 12 evaluation (§12) | test_evaluation_phase12.py (9): restart survival (fresh repo reads record), fixed-fixture reproduction (stable fixture_hash, changed fixture ⇒ different hash), uncertain/failed effects fail runs, confirmed effects succeed, scorer version recorded; test_evaluation_evidence_integration.py (3): adapter pulls REAL effect/step evidence from durable repos (PREPARED→OUTCOME_UNKNOWN via state machine), tool metrics from steps, restart-readable records; test_evaluations_api.py (4): tenant scoping 403, admin-only summary, missing repo 503 |
| Firestore parity, LIVE emulator (§3.21) | 26/26 passed against a real emulator run (not just skips): test_phase3_firestore_parity.py (4, incl. repaired capacity idempotency/fencing), test_phase8_11_firestore_parity.py (15 — effect ledger: transport-retry identity, CONCURRENT duplicate prepares → one effect, CAS version conflicts, receipt dedup, cancelled-digest reuse; browser ownership tenant/lease CAS + generation advance, strict renewal, transition matrix, expired sweep; step CAS + version conflict + DISPATCHING→OUTCOME_UNCERTAIN + sequence sort; episode claims, transition CAS, atomic counters, abandoned sweep, terminal skip), test_budget_firestore_parity.py (7). Surfaced + fixed: Firestore duplicate-key worker comparison diverged from SQLite reference |
| Firestore end-to-end (Phase 12 path) | STORAGE_BACKEND=firestore against emulator: factory initializes all Phase 5/8/10/11/12 repos, effect PREPARED→DISPATCHING→OUTCOME_UNKNOWN via state machine, step recorded, `evaluate_run_async` persists an honest record (success False, uncertain effect bound), fresh repo reads it |
| Phase 13 self-improvement (§13) | test_improvement_loop.py (17): swapped-dataset refusal, grading-hash binding, one-dataset-for-both-variants, canary cap + fraction check, critical-constraint veto (0.80→0.96 blocked), floor/regression gates, agent denial (grade/promote/propose-authorization), proposer≠approver, state-machine ordering, promotion evidence linkage, rollback restores prior ref + evidence, restart survival; test_experiments_api.py (4): admin gating, HTTP lifecycle with evidence, fail-closed 501 evaluation; test_phase8_11_firestore_parity.py (+2): lifecycle + permission/constraint matrix on Firestore |
| Phase 13 review round (A–L) | test_phase13_chat1.py (7): valid step trace w/ dispatch digest, duplicate-delivery non-inflation, lost-lease supersede, queue stranding fix, stale-worker fence + takeover, exhausted reset; test_phase13_coordinator.py (5): API promotion changes the EFFECTIVE runtime, failed activation = visible receipt, API rollback restores the pinned baseline + CONFIRMED, newer-release protection, stale-coordinator guard; readiness (5): undeclared ops, failed/stale/expired/credentialless evidence refused, passing evidence activates; typed gates (5): missing/NaN/bool metrics, no-samples, server-owned constraints, direction-aware latency; feedback identity (2): fallback attribution, idempotent application; reviewed-failures (1): real-repo clustering. Parity 21/21 live emulator |
| Phase 13 final chat (D/E + 13.9/13.14-16) | test_phase13_chat6.py (5): duplicate comparison requests → same job, crash-midway resume without rewriting evidence, stale-worker completion fence, typed-gate metrics shape, shadow-observed requires recorded executions; test_experiments_api.py: evaluate returns a durable job identity / refuses invented inputs (422); browser validator refusals + effect-persistence gate (chat1 +3) |
| Phase 13 completion (13.10–13.33) | test_phase13_chat7.py (12): manifest pinning/rejection/mutation, held-out draft-proof + families, stage gates, metering, redaction, flags, retention; test_phase13_chat8.py (6): verified-only recovery credit, repeat rate, connector candidates + expansion refusal, specialist boundaries, deliverable checks; test_phase13_chat9.py (4): THE FLAGSHIP vertical journey (build→bind→test→activate→resume→recover→verify→evidence), level-3 API→worker→runtime, level-1/2 pointers |
| Phase 14 routing | test_routing_policy.py (11 — the roadmap's named file): unverified=0 reward, biased rewards separated by context, repeated failures drop rank, forbidden actions never selected, exact exploration probability, availability change, budget exhaustion → greedy, drift detection, offline comparison by category, delayed join; test_phase14_routing.py (13): exactly-once join, fallback attribution, idempotent policy application |
| Phase 15 subagents | test_phase15_agents.py (8): registry coverage + boundaries, deterministic fallback honest, fail-stands-over-judge-pass, deterministic create race (dedup), baseline case set, model proposals recorded, registry delegation, unknown-role refusal |
| Phases 1–2 subphase audit | test_phase1_2_matrix_gaps.py (7): re-add-member role preservation (invitation service refuses), UTC period + admission-period settlement, negative/zero/reduced-limit rows, settings semantics; §2.0 defect probes re-run FIXED (fresh-row cap, duplicate settlement, cross-connection visibility) |
| Phase 2 deep verification | test_phase2_deep.py (11): overrun unclipped + event-trail surfacing, dispatching crash window → reconciliation candidate, uncertainty stays counted, replay idempotent, SETTLEMENT_CONFLICT, missing-reservation error, release idempotency, fallback distinct attempt |
| Phases 1/3/4/5 deep-audit fixes | invitation-expiry hardening (both stores), RUNNING→EXPIRED transition, backend-neutral command queries (C12), CAS-guarded reconcile_uncertain (§5.11), capacity renew/release ownership-verified equality (§3.11/§4.10 — the empty-worker-id bypass), webhook replay-key release on failed acceptance (§4.16) |
| Phases 7–21 audit | Phase 7 content-binding probes (altered code denied, foreign-tenant replay denied, dependency exactness); Phases 8–11 suites (112 tests); Phases 16–21 suites (100+ tests) |
| Full suite | 916 passed, 0 failed, 48 skipped (27 environment-gated + 21 pre-existing gates) — 4 Nov 2026, working tree with v31.0.0 changes |
| Full suite (prior revision) | 916 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v31 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 911 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v30 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 911 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v29 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 900 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v28.1 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 893 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v28 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 885 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v27 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 874 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v26 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 852 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v25 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 842 passed, 0 failed, 48 skipped — 3 Nov 2026, pre-v24 tree (historical attribution, §13.0) |
| Full suite (prior revision) | 709 passed, 0 failed, 44 skipped — 2 Nov 2026, pre-v23 tree (historical attribution, §13.0) |
| Load burst (§3.22-16) | `scripts/loadtest_admission.py`: 8 OS processes x 50 attempts, allowance 25 → **exactly 25 admitted** (400 attempts, 0.08s); capacity cap 15 → **exactly 15 leases** across 400 distinct executions (0.24s). Recorded 31 Oct 2026 |
| Frontend overload states (§3.19) | `frontend/e2e/admission-and-queue.spec.ts` — 4 Playwright tests: rate-limit notice + preserved draft, 503 outage state, retry-from-notice success flow, queue panel backed by real run id. All passing |
| Admin policy operations (§3.20) | `POST /admin/admission/policies/{group}/disable|enable` — audited (`ADMISSION_POLICY_DISABLED/ENABLED`), reversible, explicit disabled state; no unaudited reset-all endpoint |
| Agent-requested builds use the same gate (§3.16) | `test_connector_build_service.py`: both origins share one allowance; QUEUE_FULL refusal at tenant cap; identical build dedups across surfaces; enqueue failure settles the command |
| Webhook command admission (§3.15) | `test_webhook_admission.py`: owner at pending cap → 429 QUEUE_FULL; signed replay → 409, one logical run |
| SSE connection capacity (§3.2) | `event_stream` policy enforced on `/workflows/{id}/events` |
| Firestore parity (§3.21) | `test_phase3_firestore_parity.py` (4 tests; emulator-gated, skips without `FIRESTORE_EMULATOR_HOST`) |
| Identity guarantees (§3.7) | `test_admission_policy.py`: forged X-Forwarded-For never changes identity; all API keys of one owner resolve to one allowance; one tenant's flood leaves others' allowance intact |

## Review amendment — 31 October 2026

The blanket remediation claim below has been withdrawn. The prior rows record
reported historical evidence, not a new full-suite run during this documentation
update. Review baseline: committed v5.5.0 (`3ef8063`), with concurrent working-tree
fixes not certified here. See [current status](implementation-status.md) and the
[22-phase file-by-file roadmap](full-stack-roadmap.md).

| Finding/gate | Revised scope | Acceptance still needed |
|---|---|---|
| F01 invitations | Flow exists; legacy direct-add conversion is in progress | All paths plus transactional accept/revoke/duplicate races |
| F08 usage | Ledger exists; SQLite commit fix is in progress | Separate connections/restart, atomic reservations and complete paid-call accounting |
| F09 limits | Local counters are bounded | Shared multi-replica enforcement |
| F07 effects | Quarantine/resolution primitives exist | Actual provider receipts and controlled resolution coverage |
| G02 coverage | Execution predicate and displayed coverage diverged in a focused check | Changed definitions/credentials invalidate both |
| G03/G04 runtime | Packaging status checks exist; host execution is disabled | Content-tampering rejection and trusted isolated protocol runner |
| G07 build recovery | Both paths record metadata; orchestrator still ran inline in baseline | Unified durable dispatch and crash/resume tests |
| Browser actions | Local observe/act and challenge handling exist | Cleanup, session ownership and action-level approval coverage |
| Learning | Router records execution outcomes | Verified task rewards and real evaluation metrics |

Focused in-memory checks in the preceding review showed SQLite writes removable
by rollback, modified standalone contents still marked deployable, and invalidated
operations still counted in coverage. They are not cloud exploit or full-suite
claims. Changes arriving concurrently must be rechecked before these rows close.

Historical test totals in this packet differ by release (for example 263 and
279). Retain exact run logs and revisions rather than treating either number as
a fresh result. Configuration of a CI step is not proof that its current run passed.

---

## 1. Identity and authorization

| Evidence required | Status | Where |
|---|---|---|
| Disabled users receive no tokens via password login | **Verified (local)** — HTTP regression | `backend/tests/test_audit_security.py` |
| Disabled users receive no tokens via Google login | **Verified (local)** — route regression with mocked id_token verification | `backend/tests/test_audit_security.py` |
| Disabled/refresh paths enforce account status | **Verified (local)** | `backend/tests/test_auth_http.py` |
| Role changes take effect immediately (no stale claims) | **Verified (local)** | `backend/tests/test_auth_http.py`, `test_audit_security.py` |
| Revocation enforced; denylist outage fails closed (503) | **Verified (local)** | `backend/tests/test_audit_security.py` |
| Membership creation never mutates roles (router + repository, atomic) | **Verified (local)** | `backend/tests/test_audit_security.py` |
| Owner-only role-change holds on every path | **Verified (local)** + router guards | `backend/api/routers/orgs.py`, `test_orgs_rbac.py` |
| Accepted-invitation lifecycle (invite → accept) | **Partial; reopened** — basic invitation tests exist; legacy path and transaction/concurrency acceptance remain | `backend/tests/test_org_invitations.py`, `dashboard/orgs/page.tsx` |
| SSO / MFA / provisioning | **Pending** (enterprise option) | — |

## 2. Tenant isolation

| Evidence required | Status | Where |
|---|---|---|
| Oversight excludes personal and other-org runs | **Verified (local)** — runs scoped to explicit `org_id` ownership; payload sanitization asserted | `backend/tests/test_audit_security.py` |
| Explicit organization ownership on shared resources | **Partially verified** — `WorkflowRun.org_id` ownership slice shipped; org-owned resources beyond runs (credentials, schedules, artifacts) remain user-scoped | `backend/models/schemas.py`, migration in `sqlite/database.py` |
| Cross-tenant isolation for memory/search/artifacts | **Pending** — needs the adversarial matrix run across every retrieval path | — |

## 3. Execution safety

| Evidence required | Status | Where |
|---|---|---|
| Expired RUNNING tasks reclaimable (Firestore parity) | **Verified by source + contract tests** — live kill-test requires cloud | `backend/repositories/firestore/workflow_repository.py`, `test_chaos_recovery.py` (SQLite path) |
| Completed local work suppresses duplicate execution | **Verified (local)** — stack-level test | `backend/tests/test_chaos_recovery.py` |
| Old execution cannot overwrite a reclaimed task (fencing) | **Verified (local)** | `backend/tests/test_chaos_recovery.py` |
| Uncertain-outcome handling | **Verified (local)** — uncertain state quarantines ambiguous outcomes and crash-orphaned locks (worker sweep); replay blocked with explicit reconciliation message; operator/provider receipt resolves via reconcile_uncertain | `backend/tests/test_uncertain_reconciliation.py` |
| Provider-specific reconciliation receipts | **Pending** — requires real provider accounts | — |
| Connector lifecycle: untested revisions cannot execute | **Verified (local)** — per-operation evidence + trust-tier gate | `backend/tests/test_connector_coverage.py`, `test_audit_security.py` (G01/G02) |
| Standalone packaging protocol-status checks | **Partial** — tests cover status flags; content-bound evidence and isolated runtime remain | `backend/tests/test_standalone_packaging.py` (G03) |
| Provenance labels (official/extracted/inferred) | **Verified (local)** | `backend/tests/test_connector_coverage.py` (G06) |
| Both build entry points record build metadata | **Partial** — equivalent queued execution/restart semantics not established | `backend/tests/test_connector_build_service.py` (G07) |
| **Self-extension loop (page-48 happy path)**: waiting run → durable build (origin + parent link) → adapter from real spec → live probe → verified registration → original run resumes and completes → auditable record | **Verified (local)** — full lifecycle integration test | `backend/tests/test_self_extension_loop.py` |
| Deliverable artifact validates and preserves user facts | **Verified (local)** — rendered HTML opens, contains actual facts, no placeholders; invented employers flagged; ATS score labeled heuristic | `backend/tests/test_resume_deliverable.py` (G08) |
| Release gate configuration | **Configured** — API image pytest, worker import smoke, frontend lint/unit/typecheck; current run evidence and real-backend browser gates remain | `cloudbuild.yaml`, `cloudbuild.frontend.yaml` (F16) |
| Resume deliverable grounding (no invented employers/degrees unnoticed) | **Verified (local)** — deterministic check + heuristic score label | `backend/services/resume_builder.py` (G08) |

## 4. Durability and shared controls

| Evidence required | Status | Where |
|---|---|---|
| Usage ledger persists and enforces a cross-process cap | **Reopened** — commit fix in progress; existing test shares a connection; reservation coverage required | `backend/tests/test_usage_ledger.py` (F08) |
| Ledger read failures are explicit (503) | **Narrow local evidence** — recording failures and missing accounting need separate coverage | `backend/tests/test_usage_ledger.py` |
| Rate limiting holds across replicas | **Partially closed** — trusted-proxy parsing, bounded state, group quotas done; *distributed* counters need Cloud Armor or a shared store (deployment-stage) | `backend/security/rate_limiter.py` |
| Blog/contact submissions durable + validated + rate-limited | **Verified (local)** — backend content repo; restart/scaling test requires deployment | `backend/api/routers/blogs.py`, `contact.py` |
| Dead-letter quarantine configured | **Verified in Terraform** — DLQ topics, quarantine subscriptions, bounded retries; alerting + replay drill are deployment-stage | `terraform/modules/pubsub/main.tf` |
| Budget reservations reconcile against provider usage | **Pending acceptance** — implement/verify reservation wiring locally; invoice comparison additionally needs provider access | — |

## 5. Frontend acceptance

| Evidence required | Status | Where |
|---|---|---|
| Session refresh coordination and 204 handling | **Partial** — local tests exist; real two-tab and unsupported-Web-Locks acceptance remain | `frontend/src/lib/auth-session.ts`, `e2e/session-expiry.spec.ts` |
| OAuth configuration consistent (CSP script/frame/connect + build var + clean disabled state) | **Verified (config)** — live Google sign-in requires real client credentials | `frontend/next.config.ts`, `cloudbuild.frontend.yaml` |
| ESLint quality gate | **Verified** — 0 errors, 0 warnings | CI-able |
| Build observation stops on unmount; reload resumes from build ID | **Verified (local)** | `dashboard/integrations/create/page.tsx` (G05) |
| Production-browser journeys (every page, devices, a11y, error states) | **Pending** — requires the production candidate deployment | — |
| WCAG 2.2 AA evaluation | **Pending** — requires defined scope + assistive-technology testing | — |

## 6. Operations

| Evidence required | Status |
|---|---|
| Clean production image build + startup (API + worker) | **Verified (clean-image, local)** — API and worker images build from a clean environment; Python 3.11.16 runtime parity confirmed by running the full 279-test suite INSIDE both images; declared deps import (pubsub, firestore); `pip check` clean; startup smoke reaches `/health` 200 with keys via secret-mount; production mode without keys fails with an actionable operator message. Cloud-project startup (Pub/Sub wiring) still needs the deployment |
| Backup restoration drill | **Pending** |
| Rollback drill | **Pending** |
| Load/soak evidence | **Pending** |
| Correlated traces + alerts + runbooks | **Pending** (tracing infrastructure exists; alert routing needs deployment) |
| Independent security assessment | **Pending** — explicitly not claimed by this project |

---

## Change ledger additions (v5.5.0)
| Item | Implementation | Evidence |
|---|---|---|
| Invitation accept UI | invitee surface on the orgs page (paste token → accept) | `dashboard/orgs/page.tsx` |
| Loopback SSRF seam | numeric-loopback allowance is a TEST-ONLY flag, default strict; workflow core.http can never reach app-localhost | `openapi_parser.py` (`_LOOPBACK_TESTING`) |

## v5.6 review closure (second focused review, 31 Oct)

| Finding | Status | Evidence |
|---|---|---|
| Membership acceptance bypass (members route) | **Closed** — the route now creates a pending invitation (202); `org_repo.add_member` is never invoked by it; tests assert the smoking gun | `test_v56_review_regressions.py` |
| SQLite usage persistence (missing commit) | **Closed** — add_usage commits; cross-connection durability test (separate DatabaseManager instances) | `test_v56_review_regressions.py` |
| Budget not an atomic reservation | **Closed** — guarded-upsert reserve before every paid call, settle after; concurrent near-cap racers cannot both win; budget counts settled+holds | `test_v56_review_regressions.py`, `test_usage_ledger.py` |
| Firestore invitation races | **Closed** — accept is one transaction (invite read + membership create + status flip); token-hash doc id; deterministic pending marker for duplicate creation | `backend/repositories/firestore/sidecar.py` |
| Artifact evidence not bound to content | **Closed** — protocol results record `content_hash`; mark_deployable refuses when content changed after testing; exact dependency pins | `test_standalone_packaging.py` |
| Coverage helper stale/duplicated | **Closed** — one authoritative function, digest-verified when spec_hash/credential supplied; factory passes both | `test_connector_coverage.py` |
| Orchestrator builds not restart-safe | **Closed** — agent builds dispatch through the queued lifecycle; parent task parks as a durable approval; the worker resolves it and re-dispatches when the build lands | `test_connector_build_service.py` |
| Browser consequential actions ungated | **Closed** — form submissions and submit-control clicks pass through the persisted, argument-bound approval gate | `web_agent.py` (`_is_consequential`) |
| Browser session leaks / unbounded pauses | **Closed** — owned resources close by reference even without a registry entry; session leases (30 min) swept by the worker | `browser_sessions.py`, `worker.py` |
| Rate limiting across replicas | **Verified (local) for abuse-sensitive groups** — auth/contact/webhook/mcp_build enforce ONE durable fixed-window limit across replicas and restarts; high-volume groups stay process-local with edge enforcement recommended for deployment | `backend/tests/test_shared_limits_and_browser_verification.py` | — |
| Generated-code isolated runner | **Open (product work)** — host execution disabled; supported runner is a scoped build | — |
| Cloud recovery, load, live OAuth, a11y journeys | **Open (deployment-bound)** | — |

## v5.7 learning + recovery loop closure (31 Oct)

| Item | Status | Evidence |
|---|---|---|
| §3 self-healing loop (detect→classify→select→execute→verify→record→escalate) | **Verified (local)** — deterministic RecoveryPolicy with per-category ceilings, deadline, repeated-action detector; RecoveryWorker refuses disallowed recoveries and escalates into structured incidents with user options | `backend/tests/test_self_healing_and_learning.py` |
| §4 EvaluationService placeholders | **Closed** — records now carry the REAL per-run token spend the model client recorded; generated once per terminal run by the worker | `test_self_healing_and_learning.py` |
| §5 stage 1 outcome verification | **Closed** — the adaptive-policy reward is positive only for verified usable payloads; a silent-but-empty response records a FAILURE reward | `test_self_healing_and_learning.py`, engine post-loop gate |
| §7 explicit task states | **Partially** — engine states exist (PENDING/RUNNING/WAITING/WAITING_APPROVAL/RECOVERING/FAILED/COMPLETED); frontend badge mapping complete; "uncertain" surfaced via reconciliation states | engine + dashboard |
| §9 first subagents (Outcome Verifier / Recovery Analyst) | **Closed** — OutcomeVerifier is a bounded read-only service (typed AcceptanceCriteria → pass/needs-review/fail, never mutates output); RecoveryPolicy is the deterministic Recovery Analyst gate | `backend/tests/test_outcome_verifier.py`, `test_self_healing_and_learning.py` |

## v5.9 additions (31 Oct)
| Item | Implementation | Evidence |
|---|---|---|
| §7 explicit task states | authoritative runStateStyle mapping (uncertain/credentials/approval/recovering distinct); builds page wired | `frontend/src/lib/task-states.ts`, `frontend/tests/task-states.test.ts` |
| §7 connector clarity | lifecycle badges (adapter/standalone · tested · verified · needs review · active) on every integration card | `frontend/src/lib/task-states.ts`, `dashboard/integrations/page.tsx` |
| §9 Outcome Verifier | bounded read-only service, typed criteria, pass/needs-review/fail; output never mutated | `backend/tests/test_outcome_verifier.py` |

## Phase-1 tenancy closure (31 Oct — detailed Phase 1 roadmap)
| Item | Status | Evidence |
|---|---|---|
| 1.5 One shared invitation service | **Closed** — both creation routes delegate to `organization_invitation_service`; no route enrolls directly | `organization_invitation_service.py`, `test_org_invitations.py` |
| 1.6 Invitation uniqueness as a DB invariant | **Closed** — unique partial index on `(org_id, email) WHERE status='pending'`; concurrent duplicate creation proven with independent connections | `test_membership_concurrency.py` |
| 1.7 Transactional acceptance | **Verified (local)** — accept is one repo operation (find→pending→email-match→expiry→create-only membership→accepted); accept/revoke race proven one-outcome | `test_membership_concurrency.py` |
| 1.8 Owner invariants | **Verified (local)** — owner fixed on member/route paths; owner-only role endpoint; create-only re-adds | `test_orgs_rbac.py`, `test_audit_security.py` |
| 1.9 Explicit run ownership | **Verified (local)** — `org_id` slice; personal never inferred from membership | `test_audit_security.py` |
| 1.10 Query-scoped authorization | **Verified (local)** — artifact lists/downloads, schedules, workflows all owner-scoped (adversarial cross-tenant) | `test_tenant_isolation.py` |
| 1.11 Revocation current-state enforcement | **Verified (local)** — password/Google/refresh/API-key paths; fail-closed denylist | `test_audit_security.py`, `test_auth_http.py` |
| 1.12 Background authorization | **Partially** — engine re-loads current identity at execution; per-task org-membership recheck before consequential steps remains staged | — |
| 1.13 Frontend access-change handling | **Partially** — invitation accept UI shipped; 401 handling clears session and redirects; full stale-cache clearing on access loss is frontend journey work | `dashboard/orgs/page.tsx`, `auth-session.ts` |
| 1.15 Adversarial matrix | **Verified (local)** — tenant isolation (artifacts/schedules/credentials/oversight) + membership concurrency (independent connections) suites added | `test_tenant_isolation.py`, `test_membership_concurrency.py` |
| 1.3 Verified-recipient identity | **Verified (local)** — `email_verified` identity field (Google claims verified, local signup starts unverified); invitation acceptance gated by `REQUIRE_VERIFIED_EMAIL_FOR_INVITES` with regression tests both ways | `test_background_authorization.py` |
| 1.4 Named permission matrix | **Verified (local)** — `security/permission_matrix.py` (named actions → minimum org role; owner-only actions explicit; org role ≠ platform role) | `test_orgs_rbac.py`, `test_audit_security.py` |
| 1.12 Background authorization | **Verified (local)** — the worker re-loads the owner's CURRENT account state before executing any queued task; disabled-owner tasks are denied with an attributable `BACKGROUND_AUTHORIZATION_DENIED` audit event and acknowledged (not requeued) | `test_background_authorization.py` |
| 1.1 Authorization inventory | **Delivered** — `docs/AUTHORIZATION_INVENTORY.md`: 24 entry points × actor × resource × permission × enforcement × test | `docs/AUTHORIZATION_INVENTORY.md` |
| Ownership transfer (1.8) | **Open (staged)** — dedicated operation per matrix; owner protections enforced on every other path | — |
| 1.2 Central authorization service | **Closed** — `resource_authorization.py` (resolve_membership / require_membership / require_permission / require_resource_owner); orgs routes delegate | `test_orgs_rbac.py` + all tenancy suites |
| 1.5 Resend policy | **Closed** — resend rotates the token, extends expiry, PRESERVES role; admin invitations only the owner resends; old token dead | `test_invitation_resend_preview.py` |
| 1.7 Acceptance revalidation | **Closed** — acceptance revalidates the org still exists; invalid invitations cannot grant membership | `test_invitation_resend_preview.py` |
| 1.13 Invitation preview | **Closed** — invitee sees org name + role pre-accept, read-only, token still valid after | `test_invitation_resend_preview.py` |

## Phase-1 gap closure (31 Oct — detailed Phase 1 roadmap §1.0–1.18, final pass)

Phase 1 was previously executed end to end (v6.0.0–v6.2.0) with the evidence
rows above. This pass audited every §1.18 checklist item against the current
code and closed the items the packet itself flagged as open:

| Item | Status | Evidence |
|---|---|---|
| 1.8 Ownership transfer (was "Open (staged)") | **Closed** — dedicated owner-only route `POST /orgs/{org_id}/transfer-ownership`; repo operation performs a guarded atomic swap (promote target → owner, demote outgoing owner → admin) on an owned-connection transaction, verifies the single-owner invariant inside the transaction, and writes `ORG_OWNERSHIP_TRANSFERRED` in the same transaction. Concurrent transfers: exactly one wins. Ordinary role endpoints still cannot touch the owner row. | `test_ownership_transfer.py` (8 transfer tests incl. concurrency), `orgs.py`, `org_repository.py` + Firestore parity in `sidecar.py` |
| 1.7 owned-connection acceptance (explicit SQLite requirement) | **Closed** — `accept_invitation` now runs its full read-validate-write sequence inside `BEGIN IMMEDIATE` on a dedicated connection; the ordering defect where an already-member acceptance still flipped the invitation to `accepted` is fixed (invitation stays pending/revocable); `ORG_INVITATION_ACCEPTED` commits in the same transaction (§1.14 atomic audit) | `test_ownership_transfer.py::test_accept_leaves_invitation_pending_for_existing_member`, `::test_accept_writes_audit_inside_the_transaction`, `::test_accept_vs_revoke_race_is_one_outcome` |
| 1.12 background org-membership recheck (was "staged") | **Closed** — `worker.authorize_background_execution` (one shared gate, no mirrored test logic) denies org-owned runs whose owner lost membership or whose org was deleted (`org_membership_lost`, audited); personal runs unaffected | `worker.py`, `test_ownership_transfer.py` background suite |
| 1.13 stale frontend state on access loss (was "Partially") | **Closed (local)** — 403/404 on any org-scoped call clears the selected org, members, and oversight results, shows an actionable message, and refreshes the org list; `ApiError` now carries the HTTP status | `orgs/page.tsx` (`handleAccessLoss`), `api.ts` |
| 1.16 migration rules | **Verified (local)** — idempotent re-migration preserves rows and the uniqueness invariant; pre-org legacy runs stay personal and invisible to oversight (no retroactive sharing); pending invitations survive with the 1.3 verified-recipient gate still applied at acceptance; no ownership repair by assignment | `test_phase1_migration_validation.py` |
| Firestore parity for the new operations | **Verified (local emulator)** — `FirestoreOrgRepository.transfer_ownership` + `resend_invitation` run transactionally against the real emulator (cloud-firestore-emulator v1.22.0, firebase-tools 15.34.2). The parity run caught and fixed two real bugs: `resend_invitation` was missing from the Firestore sidecar entirely (SQLite-only parity gap), and `transfer_ownership` performed a read-after-write that Firestore transactions reject. A latent missing `firestore` import in `sidecar.py` (would have broken every Firestore acceptance) was also found and fixed | `test_phase1_firestore_parity.py` (10 tests, re-run-idempotent), `sidecar.py` |
| §1.18 checklist | **All items verified (local)** — including "SQLite and Firestore both pass the applicable contract tests": org/invitation/membership/ownership contract executed against the Firestore emulator (10 tests) alongside the budget contract (7 tests); full backend suite 427 passed, 0 skipped, with the emulator active; frontend E2E 20 passed including the 4 budget-controls journeys | this section + `AUTHORIZATION_INVENTORY.md` |

## Phase-2 budget accounting closure (31 Oct — detailed Phase 2 roadmap, §2.1–2.21)

Spec: [budget-accounting.md](budget-accounting.md). All rows are **verified (local)**
against the exact working tree summarized in the table, unless marked otherwise.

| Roadmap item | Status | Evidence |
|---|---|---|
| §2.0 Defect reproduction | **Closed** — first-insert cap bypass and duplicate settlement reproduced, then fixed structurally; permanent regression guards | `test_budget_defects.py` |
| §2.1 Guarantees documented | **Closed** — G1–G10 with the enforceable-contract boundary written down; usage never clipped | `docs/budget-accounting.md` §1 |
| §2.2 Concept separation | **Closed** — context estimator (`/usage/context`) and spending ledger (`/usage/budget`) are separate endpoints and separate UI surfaces; the fake "5h rolling window" spend figure was removed | `usage.py`, `ContextUsage.tsx`, `TokenStatusCard.tsx` |
| §2.3 Scopes and policy | **Closed** — user + platform (+org) buckets, all must pass; settings semantics (null/0/negative/ceiling clamp) validated on write | `test_budget_reservations.py::test_all_applicable_limits_must_pass`, `settings_router.py` |
| §2.4 Accounting equation | **Closed** — Available = Limit − Settled − Holds; holds include uncertain; overruns recorded in full and surfaced | `test_budget_reconciliation.py` |
| §2.5 Durable identity | **Closed** — per-attempt reservation rows with unique request keys; replay returns the same hold, digest mismatch conflicts; distinct task/call/attempt identity discipline | `test_budget_reservations.py`, `models/usage.py` |
| §2.6 State machine | **Closed** — reserved → dispatching → settled/uncertain → released with optimistic concurrency (`record_version`); expiry investigates, never auto-releases | `usage_repository.py`, `test_budget_reservations.py` |
| §2.7 Repository contract | **Closed** — `UsageRepository` protocol expanded; settle() loads the original hold (caller never supplies release amounts); typed errors distinguish exhausted/unavailable/conflict/invalid/not-found | `usage_repository.py` |
| §2.8 SQLite transactions | **Closed** — dedicated-connection `BEGIN IMMEDIATE` transactions (`DatabaseManager.write_transaction`); shared connection never used for accounting mutations; reservation+buckets+event commit atomically | `sqlite/database.py`, `test_budget_concurrency.py` |
| §2.9 Firestore parity | **Verified (local emulator)** — identical contract implemented and EXECUTED over Firestore transactions (stable IDs generated outside callbacks, reads-before-writes, read-modify-write counters); all 7 parity tests pass against cloud-firestore-emulator v1.22.0 and are re-run-idempotent (per-test clean state + fresh client, since the emulator persists data across runs like production) | `test_budget_firestore_parity.py` |
| §2.10 Budget service | **Closed** — single `BudgetService` (policy resolution, admission, dispatch intent, settlement, snapshots); usage captured before parsing so JSON failures still settle | `budget_service.py`, `test_budget_service_and_coverage.py` |
| §2.11 Provider coverage | **Closed (local)** — every supported dispatch metered at its boundary: shared text/JSON (incl. repair + Groq/Grok fallbacks), ADK Groq/HF adapters (per model invocation), embeddings, JD parser + resume tailor (converted from direct SDK to metered shared client), web-agent decisions, grounded search, vision | `test_budget_provider_coverage.py` |
| §2.12 Normalization | **Closed** — common receipt; missing provider fields stay unknown (never false zeroes); cached/reasoning not double-counted; estimate vs provider-reported labeled | `usage_normalizer.py`, normalizer tests |
| §2.13 Failure handling | **Closed** — pre-dispatch failure releases; provider rejection (429/401/404) releases with evidence; timeout/disconnect marks uncertain; settlement-storage failure preserves a recoverable hold; metered stream settles from final usage | `test_budget_service_and_coverage.py`, `test_budget_reconciliation.py` |
| §2.14 Periods | **Closed** — UTC calendar day assigned at admission; settlement stays against the original period; UI shows reset in user's timezone; outstanding-hold exposure guard (0.5 × limit) blocks hoarding | `models/usage.py`, `test_budget_service_and_coverage.py` |
| §2.15 Snapshot API | **Closed (local)** — `GET /usage/budget` (snapshot incl. explicit unavailable state) and `POST /usage/budget/check` (admission probe); structured errors with documented HTTP mapping; no public mutation endpoints | `api/routers/usage.py` |
| §2.16 Frontend | **Closed (local)** — `BudgetUsagePanel` (allowance/consumption/holds incl. uncertain subset/available/reset/quality/status), `BudgetBlockedNotice` (structured exhausted/paused messages wired into run creation), context estimate visually separated | `frontend/src/components/Budget*.tsx`, `budget-controls.spec.ts` |
| §2.17 Test matrix | **Closed (local)** — real multi-process competition (subprocess workers), independent connections, restart persistence, full acceptance matrix | `test_budget_concurrency.py` (5), `test_budget_reservations.py` (19) |
| §2.18 Migration | **Closed (local)** — legacy aggregates preserved via `legacy_seed` mirror; legacy `add_usage`/ledger remain for old readers; metered path writes consumption exactly once (no dual-write double counting) | `test_budget_reconciliation.py`, `test_budget_service_and_coverage.py` |
| §2.19 Observability | **Closed (local)** — admission/rejection/overrun/uncertain/settlement-failure counters (no user identifiers in labels); `GET /admin/budget/metrics`; operator-triggered `POST /admin/budget/reconcile`; append-only adjustment events with idempotent correction ids | `budget_service.py`, `api/routers/admin.py` |
| §2.20 Order | **Closed** — repository semantics first (steps 1–7) before instrumenting call paths (8–13), exactly as the roadmap prescribes | git history of this change |
| §2.21 Acceptance checklist | **Below** — per-check status | — |

### §2.21 completion checklist

| Check | Status |
|---|---|
| Every supported paid call obtains a reservation | **Verified (local)** — coverage suite; observation-mode escape hatch documented |
| Fresh-row and existing-row admission enforce identical limits | **Verified (local)** — dedicated-connection write lock |
| Concurrent processes cannot independently spend the same allowance | **Verified (local)** — real OS-process test |
| Every reservation has a stable identity | **Verified (local)** |
| Settlement and release are idempotent | **Verified (local)** |
| SQLite and Firestore satisfy the same contract | **Verified (local emulator)** — 7/7 budget parity + 10/10 org parity |
| Missing accounting blocks new paid work | **Verified (local)** — fail-closed 503/`ACCOUNTING_UNAVAILABLE` |
| Provider retries and fallbacks are accounted for | **Verified (local)** |
| ADK and embedding paths are covered | **Verified (local)** |
| Actual usage recorded even when output parsing fails | **Verified (local)** |
| Uncertain execution does not silently release allowance | **Verified (local)** |
| Accounting survives process termination and restart | **Verified (local)** |
| Frontend reads authoritative budget state | **Verified (local)** — unit/type checks plus the 4 budget-controls E2E journeys executed against the dev server (panel equation, unavailable-not-zero, context≠spending, blocked-notice copy) |
| Context estimates clearly separate from consumption | **Verified (local)** |
| Policy changes and corrections are authorized and audited | **Verified (local)** — admin endpoints + append-only events |
| Migration does not double-count or reset existing usage | **Verified (local)** |
| Evidence attached to the exact tested revision | **This section; revision = the Phase-2 working tree on `main` at v6.3.0** |

## Phase 14 — verified contextual routing + controlled learning (4 Nov, v33.0.0)

Status: **verified (local)** for every item below; a staged canary drill on
real traffic remains **pending** (needs a deployment, per §14.1 release
scope). The release-scoped contextual selector is OFF by default
(`ROUTING_STAGE_ENABLED` / `IMPROVEMENT_STAGES_ENABLED=routing`); the
learning/evidence paths always run.

| Item | Evidence (this repository) |
|---|---|
| Evidence register (14.0) | `test_phase14_foundation.py` (register refuses unevidenced claims; upsert + summary) |
| Release scope (14.1) | `test_phase14_foundation.py::test_contextual_selector_is_release_scoped` |
| Measured summary by category (14.2) | `test_phase14_foundation.py::test_summary_reports_quality_cost_latency_by_category`, `/usage/routing/summary` |
| Job recovery windows (14.3) | `test_phase14_job_recovery.py` (create-or-get, takeover, reconciliation, exhaustion, drain) |
| Capability-ready gate (14.4) | `test_phase14_capability_gate.py` (ladder composition incl. §14.H declarations) |
| Browser checkpoints (14.5) | `test_phase14_browser_checkpoint.py` (durable, monotonic references) |
| Comparison pipeline (14.6) | `test_phase14_comparison_pipeline.py` (create-or-get, resume, no fabrication) |
| Activation crash-safety (14.7) | `test_phase14_receipts.py` (durable receipts, reconciliation) |
| Learning ownership (14.8) | `test_phase14_learning_ownership.py` (AST guard: sanctioned writers only) |
| Catalog + eligibility (14.9/14.10) | `test_phase14_foundation.py` (visible availability, pinned-model refusal, one source) |
| Context schema + decision record (14.11/14.12) | `test_phase14_foundation.py` (validation + full-field persistence) |
| ONE selector (14.13) | `test_phase14_foundation.py::test_the_engine_has_a_single_selection_site` (AST guard) |
| Probabilities/exploration/attribution/spec/rewards (14.14–14.18) | `test_phase14_parity_and_rewards.py`, `test_routing_policy.py`, `test_phase14_rewards.py` |
| Storage parity (14.19) | `test_phase14_parity_and_rewards.py` (routing/stats/jobs method surfaces) |
| Cost + quarantine (14.20/14.21) | `test_phase14_rewards.py` (cost EWMA, malformed-event quarantine) |
| Supported estimator (14.22) | `test_phase14_rewards.py::test_offline_comparison_names_its_estimator` (IPS vs descriptive) |
| Baselines/uncertainty/artifacts (14.23–14.25) | `test_phase14_eval_governance.py` (fixed baseline, CI95, exact artifact restore) |
| Shadow/canary/adapter/drift (14.26–14.29) | `test_phase14_rollout_gates.py` (measured observations, fraction, deterministic variants, count-rotated windows) |
| Recovery/tools/browser/specialists (14.30–14.33) | `test_phase14_strategies.py` (verified-only credit, unavailable revisions, honest recovery levels, measurable contracts) |
| Learning dashboard (14.34) | operations page: routing health + drift + leaderboard; `getRoutingHealth` typed helper |
| React reliability + 3D polish (14.35/14.36) | `frontend/tests/phase14-reliability.test.ts` (14 vitest tests; zoom clamp contract) |
| Flagship journey (14.37) | `test_phase14_flagship.py` (complete vertical, idempotent end to end) |
| RL research lane (14.39) | `test_rl_research_lane.py` (AST guard: no lane imports in the production path) |
| Findings A–K | `test_phase14_rewards.py` (A/B/C/D), `test_phase14_job_fencing.py` (G), `test_phase14_operation_propagation.py` (H), `test_phase14_browser_authority.py` (I), `test_phase14_receipts.py` (J), `frontend` receipt consumption (K) |

## Phase 15 — bounded specialists and reliable coordination (4 Nov, v34.0.0)

Status: **verified (local)** for every item below; live multi-agent
staging drills remain **pending** (need a deployment). The specialist
release stages default to read-only (`SPECIALIST_STAGES_ENABLED`) —
serving stages are opt-in, stage by stage.

| Item | Evidence (this repository) |
|---|---|
| Inventory + delegation policy (15.0/15.1) | `test_phase15_foundation.py` |
| Hierarchy + complete contracts (15.2/15.3) | `test_phase15_foundation.py` |
| Authority intersection + recheck (15.4/15.5) | `test_phase15_foundation.py`, `test_phase15_agents.py` |
| Bounds/budgets/dispatch/fencing (15.6–15.9) | `test_phase15_coordination.py` |
| Scheduling + shared resources (15.10/15.11) | `test_phase15_coordination.py` |
| Handoffs/boundary/verification (15.12–15.14) | `test_phase15_handoffs.py`, `test_phase15_agents.py` |
| Partial completion/cancellation/recovery (15.15–15.17) | `test_phase15_handoffs.py` |
| Builder/validator/browser/deliverable (15.18–15.21) | `test_phase15_specialist_paths.py` |
| Attribution/evaluation/learned selection (15.22–15.24) | `test_phase15_learning.py` |
| Workspace UI + reliability + graph (15.25–15.27) | `/workflows/{id}/delegations`; `frontend/tests/phase15-coordination.test.ts` |
| Parity/flagship/progressive release (15.28–15.30) | `test_phase15_release.py` |

## Release claim this packet supports today
| Item | Implementation | Evidence |
|---|---|---|
| Shared durable rate limiter | guarded-upsert fixed windows (SQLite/Firestore) for auth/contact/webhook/mcp_build; cross-replica + cross-restart tests; worker purges expired windows | `test_shared_limits_and_browser_verification.py` |
| §6 browser verification + stale detection | done-success requires effective steps; repeated same-action-no-page-change twice raises for recovery | `web_agent.py` |

## Release claim this packet supports today

The repository has substantial implementation and previously reported local
regression/image evidence. Findings listed in the amendment remain open or in
progress. Neither “all F01–F16/G01–G08 closed” nor “only deployment remains” is
supported. Required source fixes and candidate-specific operational evidence must
pass before the selected launch scope is accepted. No production certification
is claimed.

## Historical implementation ledger (qualified by the amendment above)

| Finding | Implementation | Regression evidence |
|---|---|---|
| F01 org oversight | org-scoped runs only (`org_id` ownership) + accepted-invitation lifecycle | `test_audit_security.py`, `test_org_invitations.py` |
| F02 role bypass | 409 on re-add; atomic create-only repos | `test_audit_security.py` |
| F03 disabled login | password + Google + refresh enforcement | `test_audit_security.py`, `test_auth_http.py` |
| F04 revocation | fail-closed 503; current-state authorization | `test_audit_security.py`, `test_auth_http.py` |
| F05 Pub/Sub dep | declared in requirements; imports verified in the clean image | `requirements.txt` + clean-image run |
| F06 Firestore leases | expired-RUNNING reclaim + fencing | `test_chaos_recovery.py`, source parity check |
| F07 exactly-once | uncertain quarantine + crash sweep + explicit reconciliation | `test_uncertain_reconciliation.py` |
| F08 budgets | durable ledger + explicit failures | `test_usage_ledger.py` |
| F09 rate limits | proxy parsing + bounded state + shared durable limits for sensitive groups | `test_shared_limits_and_browser_verification.py` |
| F10 Firestore parity | activation, tx-uniqueness, tx-counters, db id | `test_cloud_contracts.py` |
| F11 refresh race | single-flight + Web Locks + 204 | `e2e/session-expiry.spec.ts` |
| F12 OAuth/CSP | GIS origins + build var + disabled state | config verification |
| F13 local JSON | durable content repo + validation + limits | routers + e2e |
| F14 sandbox | redaction, python3, clear unsupported failure, host-execution prevention | `test_cloud_contracts.py` |
| F15 dead letters | DLQ + quarantine + bounded retries | Terraform |
| F16 release gates | hermetic tests, lint clean, adversarial suites, clean-image parity (279/279 in-image on 3.11) | full suite + container run |
| G01 execution gate | trust_tier + per-operation evidence | `test_connector_coverage.py` |
| G02 coverage | explicit evidence + coverage reporting | `test_connector_coverage.py` |
| G03 artifacts | adapter default; standalone gated by protocol tests | `test_standalone_packaging.py` |
| G05 build polling | abort on unmount | source + e2e |
| G06 provenance | stamped, preserved, trust ceiling | `test_connector_coverage.py` |
| G07 durable builds | shared service, origin + parent run | `test_connector_build_service.py` |
| G08 resume | grounding check + heuristic label + deliverable validation | `resume_builder.py`, `test_resume_deliverable.py` |
