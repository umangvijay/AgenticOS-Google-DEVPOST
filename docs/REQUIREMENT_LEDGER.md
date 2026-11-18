# Requirement & Evidence Ledger (§12.0)

**Status ladder** — no requirement is complete merely because a file,
schema, enum, or test exists:

`PRESENT` (code exists) → `INTEGRATED` (a production caller uses it) →
`VERIFIED LOCALLY` (tests executed against the real contract) →
`VERIFIED IN STAGING` (deployed-replica evidence). Also `BLOCKED`
(needs external infrastructure) and `EXCLUDED` (explicit release scope
decision).

Snapshot: working tree 2 Nov 2026, full suite **737 passed / 0 failed /
45 skipped**; Firestore parity **19/19 against a live emulator**.

## Review findings A–I (corrected Phase 12 defects)

| # | Finding | Correction | Status |
|---|---|---|---|
| A | Engine verified the OLD output, read criteria from `input_data`, and let `needs_review` complete | Completion boundary rewritten: sanitize NEW result → verify against `task.acceptance_criteria` (input_data only as legacy fallback) → every verdict maps to a permitted state (`fail`→FAILED, `needs_review`→NEEDS_REVIEW, pass→COMPLETED); verdict + reason persist with the task | VERIFIED LOCALLY (`test_review_batch1_outcomes.py::test_failing_criteria_fail_the_task_with_current_result`, `test_needs_review_never_becomes_completed_run`) |
| B | Runner computed verdicts but ignored them for overall success | Overall success now REQUIRES every task verdict `pass` plus zero blocking effect interpretations | VERIFIED LOCALLY (`test_review_batch1_outcomes.py`) |
| C | Effects joined to tasks by substring inside effect ids | Explicit `task_id`/`run_id` join fields on effect rows; unattributable effects still gate the run | VERIFIED LOCALLY (`test_effect_cannot_satisfy_a_different_task_via_id_substring`, `test_unattributable_effect_still_gates_the_run`) |
| D | Metrics: counter drained, step count as tool calls, seconds-as-ms, zeros for unknown, configured model, completed-with-recovery counted as success | Runner is pure (§12.10); usage joins the durable ledger (`usage_summary_for_run`, both stores); tool calls = validated dispatch steps; latency in explicit ms; unavailable measurements are None; `models_used` from ledger; successful recoveries require verified verdicts | VERIFIED LOCALLY (`test_review_batch1_outcomes.py::test_usage_summary_for_run_joins_the_ledger`, `test_s4_evaluation_record_carries_real_per_run_tokens`) |
| E | Duplicate terminal events created second evaluations; SQLite upsert could rewrite records | Create-or-get identity (run_id, evaluator_version) in BOTH stores; create-only rows; new scorer → linked revision (`previous_evaluation_id`); SQLite migration rebuilds NOT NULL measurement columns safely | VERIFIED LOCALLY (`test_duplicate_delivery_returns_the_original`, `test_new_scorer_version_creates_linked_revision`) |
| F | EvaluationPanel could overclaim and hid fetch errors | Panel renders verdicts per task, uncertain/failed effect counts, pending/absent evaluation states; refresh logic — see 12.31 below | INTEGRATED (frontend; Playwright evidence pending) |
| G | Recovery worker claimed via task counter; episode transitions ignored owner/lease | Episode repo is the claim authority (atomic get-or-create via partial unique index / Firestore anchor; CAS claim; worker refuses recovery without it); protected transitions require current owner + live lease + generation; persistent absolute deadline escalates | VERIFIED LOCALLY (`test_recovery_worker_acceptance.py` incl. competing workers + deadline; emulator parity) |
| H | Build path dropped `api_credential_name`, lacked org context, resumed parent on raw build success | Context propagation end-to-end (command, build record, child run `org_id`, task input) carrying the credential-BINDING reference only; §12.7 readiness ladder gates the parent resume with the precise next action | VERIFIED LOCALLY (`test_review_batch2_build_contracts.py`) |
| I | Build initial work was a SQLite connection callback — rejected by the Firestore command repo | Backend-neutral `initial_work_items` (parent_wait / build_record / workflow_run_with_task); the service translates to a SQLite callback or deterministic Firestore triples with equivalent create-if-absent semantics | VERIFIED LOCALLY (`test_translator_produces_equivalent_sqlite_and_firestore_work`, `test_command_accepts_neutral_items_on_sqlite`) |

## Subphase ledger (12.x)

| Item | Scope | Status |
|---|---|---|
| 12.0 | This ledger | VERIFIED LOCALLY (this file; refreshed per batch) |
| 12.1 | First supported scope | INTEGRATED — journeys enumerated below in "Supported scope"; fixtures = engine/service test suites; end-to-end benchmark 12.21 outstanding |
| 12.2 | Completion boundary | VERIFIED LOCALLY (finding A) |
| 12.3 | Execution vs outcome status | VERIFIED LOCALLY — task/run `NEEDS_REVIEW`, `TASK_NEEDS_REVIEW`/`WORKFLOW_NEEDS_REVIEW` events, command parks WAITING (not FAILED); evaluation lifecycle distinct (`pending/recorded/unavailable/superseded` surfaced by API) |
| 12.4 | Effect decision table | VERIFIED LOCALLY — every state + unknown values + mixed results (`required_write_interpretation`) |
| 12.5 | Authoritative recovery episodes | VERIFIED LOCALLY (finding G) |
| 12.6 | Build context propagation | VERIFIED LOCALLY (finding H) |
| 12.7 | Capability-ready gate | VERIFIED LOCALLY (finding H) |
| 12.8 | Shared storage contract | VERIFIED LOCALLY — effects, steps, episodes, sessions, evaluations, experiments, budget, admission all have SQLite+Firestore implementations; parity suites 19/19 on a live emulator; 12.28-acceptance (tenant filtering on every new endpoint) enforced in evaluations/experiments/recovery routers |
| 12.9 | Evaluation record model | VERIFIED LOCALLY — ownership/identity/versions/measurements with None-unavailable; review/lineage fields present (`previous_evaluation_id`) |
| 12.10 | Collection vs scoring split | VERIFIED LOCALLY — `_gather_run_evidence`/`_gather_usage` (authorized durable reads, first-class `evidence_gaps`) vs pure `evaluate_run`; determinism test: fixed fixtures reproduce (`test_fixed_fixture_reproduces_same_evaluation`) |
| 12.11 | Durable evaluation jobs | VERIFIED LOCALLY — duplicate delivery returns the original; new scorer links a revision; the durable JOB queue (`evaluation_jobs`: create-or-get identity, CAS leases, bounded requeue, visible exhaustion) now carries terminal-run evaluation crash-safely |
| 12.12 | Immutable history | VERIFIED LOCALLY (finding E) |
| 12.13 | Explicit identity joins | VERIFIED LOCALLY (finding C) |
| 12.14 | Durable usage ledger cost | VERIFIED LOCALLY — measured/estimated/reserved/uncertain + pricing version + models; per-direction token split waits on normalized settlement receipts |
| 12.15 | Model/tool attribution | VERIFIED LOCALLY — `models_used` from reservations; tool calls = validated dispatches; error rates categorized from step error codes (structured vocabulary still to be tightened) |
| 12.16 | Timing units | VERIFIED LOCALLY — explicit ms; stage timings reported with explicit `None` where not yet instrumented (queue wait, approval wait) |
| 12.17 | Task-specific evaluators | INTEGRATED — criteria kinds (non_empty/contains/schema_keys/http_ok/artifact_exists) cover read/mutation/browser/report families; per-family evaluator registry is future work |
| 12.18 | Missing evidence first-class | VERIFIED LOCALLY — `evidence_gaps` distinguish repo-unavailable vs empty; gaps never improve a score |
| 12.19 | Model judges | VERIFIED LOCALLY — judge-eligible kinds only (semantic_match/readability ⇒ needs_review until judged); authoritative kinds refused; model/rubric/prompt recorded; judge pass never upgrades a deterministic fail; adversarial labeled harness gates usage |
| 12.20 | Versioned datasets | INTEGRATED — fixture hashes pin datasets in evaluations + experiments (candidate cannot tune its own held-out set) |
| 12.21 | Self-extension benchmark | BLOCKED — needs the controlled test application + real provider credentials |
| 12.22 | Connector trust per operation | INTEGRATED — validation runs are per-operation with revision/credential binding; drift invalidates (Phase 6) |
| 12.23 | Adapter vs standalone separation | INTEGRATED — Phase 7 gates: deployable label requires protocol/isolation evidence |
| 12.24 | Browser/DOM fixtures | BLOCKED — needs real-browser fixture harness (§9.20) |
| 12.25 | Bounded ReAct evaluation | VERIFIED LOCALLY — Phase 10 suites (misleading output, bounds, uncertain effects, restart persistence) |
| 12.26 | Recovery worker evaluation | VERIFIED LOCALLY — duplicate delivery, competing workers, deadline expiry, escalation; queued repair ≠ success (Finding 11) |
| 12.27 | Deliverable/resume factuality | INTEGRATED — artifact resolver-backed checks (no resolver ⇒ unverifiable); resume provenance reviewed separately |
| 12.28 | Privacy/retention | INTEGRATED — tenant scoping on evaluation/experiment/recovery APIs; retention/export policy doc outstanding |
| 12.29 | Aggregate parity | VERIFIED LOCALLY — windowed subquery in SQLite matches Firestore order+limit-first; window test added |
| 12.30 | Evaluation API | VERIFIED LOCALLY — run/record/summary with tenant scoping; pagination + evidence-completeness fields to extend next |
| 12.31 | React evaluation states | INTEGRATED — EvaluationPanel shows verdicts/effects/pending; late-arrival refresh + explicit error states to extend next |
| 12.32 | Connect-an-app flow | INTEGRATED — build lifecycle UI exists; precise per-stage copy ("Generated/Needs credentials/Read operation tested") wired into the readiness notification copy |
| 12.33 | 3D presentation | INTEGRATED — existing scene foundations preserved; telemetry-vs-animation distinction outstanding |
| 12.34 | Self-improvement foundation | VERIFIED LOCALLY — Phase 13 experiments + the executable rollout (versioned prompt store, policy snapshots, proposer pipeline, deterministic cohorts, wired rollback) — see Phase 13 rollout below |
| 12.35 | RL-ready feedback | VERIFIED LOCALLY — Phase 14: verified delayed rewards (unverified completions never positive), decision log with context/eligible/probability/policy version, coverage-aware offline comparison |
| 12.36 | Specialist-agent roles | VERIFIED LOCALLY — Phase 15: agent task contracts with per-role authority boundaries, bounded delegation (depth/fan-out/concurrency/spend), dedup, partial-failure verdicts |
| 12.37 | Acceptance matrix | PARTIAL — most rows covered by named tests above; matrix doc consolidated here |
| 12.38 | Release evidence | RELEASE_EVIDENCE.md updated per batch; deployed-replica gates remain |

## Phase 13 review-round status (3 Nov, v23.0.0)

Review findings A–L from the second Phase 13 audit, corrected and
verified. Per §13.0: statuses distinguish helpers from API-driven
workflows, and historical suite counts remain attributed to their
original revisions.

| Finding | Correction | Evidence level |
|---|---|---|
| A — step evidence ignored the state machine | Engine drives PROPOSED→VALIDATING→READY→DISPATCHING→VERIFYING→terminal with LEGAL actors; digest recorded at DISPATCH; lost leases SUPERSEDE; duplicate delivery cannot inflate | VERIFIED LOCALLY (test_phase13_chat1.py: trace, dup, lost-lease) |
| B — evaluation queue could strand work | `claim_one` (the hook claims ITS run's job), independent bounded `drain` in the housekeeping loop, lease-owner FENCING on complete, `reset_exhausted`, reconciliation for un-enqueued terminals, Firestore parity repo | VERIFIED LOCALLY (stale-worker fence, takeover-after-expiry; repo parity on emulator) |
| C — readiness overstated validation | `required_operations` travels through the durable build contract (undeclared = refuse); readiness requires PASSING, unexpired, current-revision, credential-bound runs | VERIFIED LOCALLY (5 readiness tests) |
| G — proposer read impossible records | `reviewed_failures` query (terminal, tenant-scoped, time-windowed) replaces the abandoned-episodes filter | VERIFIED LOCALLY (real-repo clustering test) |
| I — promotion gates accepted incomplete evidence | Finite-numeric metrics, real booleans, required sample counts, direction-aware regression (lower-is-better), SERVER-OWNED mandatory constraints | VERIFIED LOCALLY (5 typed-gate tests) |
| F — promotion/rollback did not change runtime | Deployment coordinator: API promotion ACTIVATES the candidate (receipt: pending→effective/failed); API rollback supplies the restore and reports rollback-CONFIRMED only when the effective version reverted | VERIFIED LOCALLY (5 coordinator tests incl. API-level) |
| H — canary not genuinely bounded | Durable `experiment_exposures` enforce the EXACT absolute cap (idempotent per subject); active experiments queried by TARGET | VERIFIED LOCALLY (cap + target-query tests) |
| J — storage/concurrency gaps | Firestore prompt activation verifies version OWNERSHIP; coordinator refuses stale expected-active-version; rollout resolves experiments by target query | VERIFIED LOCALLY (stale-coordinator test) |
| K — feedback identity | Selection MECHANISM recorded (deterministic ranked first-fit, probability 1.0 — never labeled uniform random); fallback latency measured; reward applied to the RESOLVING row only, idempotent via `policy_applied` marker | VERIFIED LOCALLY (2 feedback tests) |
| L — frontend corrections | Rollback prompt cancellation performs NO mutation; canary size = predeclared cap; no hardcoded population; deployment identity derives from the activated version | INTEGRATED (frontend; tsc clean — Playwright journey pending) |
| D — browser controls in live actions | VERIFIED LOCALLY — the FULL validator (ownership, session state, controller, staleness, origin) runs before EVERY live dispatch; consequential classification extends to deletion/publish/transaction controls outside forms and confirm/checkout URLs; a consequential action whose effect record CANNOT be persisted is STOPPED (never dispatched behind a debug log) | VERIFIED LOCALLY (3 tests: classification, structured refusals, persistence-gate) |
| E — experiment comparison 501 | VERIFIED LOCALLY — durable comparisons: create-or-get job identity per pinned revision, resumable per-case records (a crash resumes without rewriting evidence), lease-fenced completion, §13.12-shaped metrics, the API returns a job identity (with pinned cases) and refuses invented inputs | VERIFIED LOCALLY (5 chat-6 tests + API contract test) |
| Shadow execution (§13.16) | VERIFIED LOCALLY — every candidate execution on a SHADOW experiment records a MEASURED observation (outcome, latency, cost); 'shadow observed' = recorded executions, never a state label | VERIFIED LOCALLY (shadow-summary test) |
| Runtime consumption (§13.14) | VERIFIED LOCALLY — the effective prompt version persists on the execution step (policy_revision) per attempt; an experiment-assigned candidate overrides a stale `_instructions`; explicit task instructions win when no experiment applies | VERIFIED LOCALLY (resolver-shape assertions + step persistence) |
| Authority (§13.15) | VERIFIED LOCALLY — trusted `Principal` kind gates (operator/agent/system with capability sets) replace actor-name-prefix checks; legacy strings parse conservatively (agent:* ⇒ propose-only) | VERIFIED LOCALLY (all improvement-service gates converted; 27+ tests) |
| Proposal quality (§13.9) | VERIFIED LOCALLY — candidate diffs are unified diffs over ACTUAL contents; equivalent proposals (same candidate version) deduplicate | VERIFIED LOCALLY (2 tests) |

Historical suite counts (737/45 and 816/48) remain attributed to their
recorded revisions; the CURRENT tree's evidence is the run recorded in
RELEASE_EVIDENCE.md below.

## Phases 7–21 deep audit (4 Nov, v31.0.0)

| Phase | Probes | Status |
|---|---|---|
| 7 (isolated MCP) | content-hash binding (altered code → DENIED_CAPABILITY_REPLAY), foreign-tenant replay → denied, dependency locks (exact hashes; extra/missing deps rejected), attestation content digests | VERIFIED LOCALLY (36 suite tests + probes) |
| 8 (browser sessions) | ownership, cleanup, consequential actions, effect persistence gate | VERIFIED LOCALLY (42 browser tests + the 13.4 deep-audit fixes) |
| 9 (DOM targeting) | semantic resolution, staleness, pipeline enforcement, outcome levels | VERIFIED LOCALLY (23 targeting tests + 4 concurrency) |
| 10 (verified ReAct) | step digests, untrusted content, bounded loops, outcome-gated completion | VERIFIED LOCALLY (32 ReAct tests) |
| 11 (self-healing) | episode state machine, claims, verified-only credit, measured rates | VERIFIED LOCALLY (15 recovery tests + the §13.22 metrics) |
| 16 (memory lifecycle) | five classes, ownership-first retrieval, supersede-with-history, correct/export/forget | VERIFIED LOCALLY (13 tests) |
| 17 (deliverable assurance) | provenance, checksums, artifact verifier | VERIFIED LOCALLY (6 tests + deliverable_verification service) |
| 18 (React reliability) | run/outcome state model, useRunEvents, draft preservation, timeouts | INTEGRATED (tsc clean; Playwright journey pending) |
| 19 (design/3D) | motion policy, scene labels, reduced-motion | INTEGRATED (foundations preserved) |
| 20 (observability) | correlation, operational-health aggregate, drift panel | VERIFIED LOCALLY (/admin/operational-health + /usage/routing/drift); deployment drills BLOCKED |
| 21 (product expansion) | templates, dry runs, inbox, comparison | VERIFIED LOCALLY (11 tests); enterprise items per-need |

## Phases 3–6 deep audit (3 Nov, v29.0.0)

| Phase | Named defects | Status |
|---|---|---|
| 3 (3.0–3.23) | cleanup-unit bug, zero-limit admission, ownership-verified renew/release | FIXED + regression-tested (76 suite tests + test_phase3_6_deep.py): the renew/release `(worker_id='' OR worker_id=?)` bypass — an empty stored id accepted ANY caller's renewal — replaced with STRICT equality on BOTH stores (SQLite WHERE + Firestore `_verify_renewable`/release); empty-id only matches empty-caller; stale-generation renewals rejected |
| 4 (4.0–4.27) | C12 + §4.16 replay-key consumption | CLOSED — backend-neutral queries on both stores; RUNNING→EXPIRED; the webhook replay-key RELEASE on failed acceptance (`release_replay_key`: running/paused entries releasable, accepted deliveries never) — a delivery's retry identity survives a queue-full rejection |
| 4 (4.0–4.27, C1–C16) | C12 SQLite-specific queries | CLOSED — backend-neutral `find_deadline_expired`/`find_unacknowledged_cancellations`/`expire_command`/`command_events` on BOTH stores; RUNNING→EXPIRED transition added (§4.3); C1–C11, C13–C16 verified closed in earlier rounds |
| 5 (5.0–5.23) | §5.11 unguarded reconcile | CLOSED — CAS-guarded `reconcile_uncertain` on the legacy ledger (one winner per resolution); the new effect ledger's CAS verified (test_effect_dispatch: concurrent resolutions 1 winner/1 conflict) |
| 6 (6.1–6.24) | §6.4/6.5/6.7/6.12 | VERIFIED — provenance defaults to unknown, canonical revision identity (sorted, evidence-excluded), credential bindings with authorization_epoch, eligibility evaluator shared by execution/UI/coverage (39 suite tests) |

## Phases 1–2 subphase ledger — the original detailed roadmaps (3 Nov, v28.0.0)

### Phase 1 — identity, invitations, tenant isolation (1.0–1.18)

| Subphase | Status | Evidence |
|---|---|---|
| 1.0 outcome guarantees | VERIFIED LOCALLY | adversarial + concurrency suites (59 tests across 9 files) |
| 1.1 entry-point inventory | VERIFIED LOCALLY | test_tenant_isolation.py, test_background_authorization.py |
| 1.2 auth vs authorization | VERIFIED LOCALLY | cross-user denial in tenant-isolation suite |
| 1.3 identity model | VERIFIED LOCALLY | user identity columns + invitation matching (test_org_invitations.py) |
| 1.4 role matrix | VERIFIED LOCALLY | test_orgs_rbac.py (owner/admin/member/viewer routes) |
| 1.5 unified invitation creation | VERIFIED LOCALLY | legacy members route → 202 invitation_pending (orgs.py:137); shared service |
| 1.6 uniqueness invariant | VERIFIED LOCALLY | partial UNIQUE INDEX on (org_id, email) WHERE status='pending' |
| 1.7 transactional acceptance | VERIFIED LOCALLY | BEGIN IMMEDIATE owned transaction + audit in-tx (org_repository.py:108) |
| 1.8 role changes/removal | VERIFIED LOCALLY | owner protections in remove/role endpoints (test_orgs_rbac.py) |
| 1.9 resource ownership | VERIFIED LOCALLY | org_id on runs; personal filtering (tenant isolation) |
| 1.10 query-level authorization | VERIFIED LOCALLY | org oversight filters by explicit ownership |
| 1.11 revocation | VERIFIED LOCALLY | disable/enable endpoints + token denylist (test_auth_http.py) |
| 1.12 background authorization | VERIFIED LOCALLY | authorize_background_execution on every task (worker) |
| 1.13 frontend access model | INTEGRATED | orgs page + invitations UI; access-loss flows |
| 1.14 audit events | VERIFIED LOCALLY | INVITATION_*/MEMBER_* events; audit in the acceptance transaction |
| 1.15 adversarial matrix | VERIFIED LOCALLY | re-add-member role preservation (test_phase1_2_matrix_gaps.py) + the 59-test suites |
| 1.16 migration | VERIFIED LOCALLY | migrations additive; no sharing inferred |
| 1.17 order | VERIFIED LOCALLY | the recorded build order matches |
| 1.18 completion checklist | VERIFIED LOCALLY | the checklist rows above |

### Phase 2 — atomic spending reservations (2.0–2.21)

| Subphase | Status | Evidence |
|---|---|---|
| 2.0 defect closure | VERIFIED LOCALLY | fresh-row cap + duplicate-settlement probes (both fixed; probe run 3 Nov) |
| 2.1 guarantees | VERIFIED LOCALLY | docs/budget-accounting.md |
| 2.2 context vs spend separation | VERIFIED LOCALLY | /usage/context separate from /usage/budget |
| 2.3 scopes + settings semantics | VERIFIED LOCALLY | platform/user buckets; null/0/negative/ceiling validation (test_phase1_2_matrix_gaps.py) |
| 2.4 accounting equation | VERIFIED LOCALLY | available = limit − settled − holds (snapshot) |
| 2.5 reservation identity | VERIFIED LOCALLY | request_key/digest; task/call/attempt ids distinct |
| 2.6 state machine | VERIFIED LOCALLY | reserved→dispatching→settled/uncertain/released (test_budget_reservations.py) |
| 2.7 repository contract | VERIFIED LOCALLY | reserve/mark_dispatching/settle/release/mark_uncertain/reconcile |
| 2.8 SQLite transactions | VERIFIED LOCALLY | BEGIN IMMEDIATE owned txns; cross-connection visibility probe |
| 2.9 Firestore parity | VERIFIED LOCALLY | 7 parity tests on a live emulator |
| 2.10 budget service | VERIFIED LOCALLY | resolve_buckets, metered attempts, normalization |
| 2.11 provider boundary coverage | VERIFIED LOCALLY | _metered_attempt in gemini/groq/huggingface/embeddings; resume/jd route through generate_json |
| 2.12 usage normalization | VERIFIED LOCALLY | usage_normalizer.py; provider-reported vs estimated |
| 2.13 failure handling | VERIFIED LOCALLY | uncertainty preserved; settlement idempotent (reconciliation suite) |
| 2.14 UTC periods | VERIFIED LOCALLY | utc_period_day; settlement charges the ADMISSION period (matrix test) |
| 2.15 budget snapshot API | VERIFIED LOCALLY | GET /usage/budget; typed errors |
| 2.16 frontend | INTEGRATED | BudgetUsagePanel + BudgetBlockedNotice wired into operations/composer |
| 2.17 test matrix | VERIFIED LOCALLY | 6 named budget suites + matrix rows + the DEEP acceptance rows (test_phase2_deep.py): overrun recorded-never-clipped + surfaced in the event trail, overrun blocks new admissions, dispatching crash window preserves the hold, uncertain stays counted, uncertain→settled reconciliation, replay idempotent, release idempotent, conflicting settlement = SETTLEMENT_CONFLICT, missing reservation = error not consumption, fallback = distinct attempt reservation |
| 2.6 state machine (deep) | VERIFIED LOCALLY | expiry → INVESTIGATION (reconciliation candidates list), never unconditional release; uncertain stays counted against the allowance until resolved |
| 2.18 migration | VERIFIED LOCALLY | legacy ledger preserved as consumption; no dual-write double-count |
| 2.19 observability | VERIFIED LOCALLY | budget_metrics (admissions/rejections/overruns) |
| 2.20 order | VERIFIED LOCALLY | contract-first order followed |
| 2.21 completion checklist | VERIFIED LOCALLY | the rows above; both §2.0 defects re-probed fixed |

## Phase 15 — specialized subagents (3 Nov, v27.0.0)

| Roadmap item | Status | Evidence |
|---|---|---|
| Outcome Verifier agent | VERIFIED LOCALLY | evidence-only contract runner; deterministic fallback honest; fail stands over judge pass; dedup + typed outputs (test_phase15_agents.py) |
| Connector Validator agent | VERIFIED LOCALLY | proposes cases (baseline set without a model); §6 pipeline executes; proposals recorded as typed contract output |
| agent_task_contracts.py | VERIFIED LOCALLY | SPECIALIST_REGISTRY (7 roles, boundaries + schemas), delegate_specialist, unknown-role refusal |
| Engine delegation | VERIFIED LOCALLY | needs_review → verifier specialist via contract (best-effort, deterministic stands) |
| Task contracts (§15.4) | VERIFIED LOCALLY | Phase 13.28 chat-8 suite (bounds, dedup, cancellation propagation) |
| Measured specialization | PARTIAL | the §15.7 comparison (specialists vs single-agent) needs the held-out comparison runs — recorded for the next evidence pass |

## Phase 14 — contextual routing & advanced RL (3 Nov, v26.0.0)

| Roadmap item | Status | Evidence |
|---|---|---|
| 1. Reward attribution (verified outcomes) | VERIFIED LOCALLY | test_routing_policy.py: unverified completion = 0 reward; delayed join drives policy + contextual learning; test_phase14_routing.py: exactly-once join, fallback attribution, idempotent application |
| 2. Context + candidate filtering | VERIFIED LOCALLY | context-bucketed stats (category × capability × size); biased rewards separated by context; candidates pre-filtered (never see forbidden actions) |
| 3. Logging (context/actions/probability/policy/missing) | VERIFIED LOCALLY | decisions log mechanism + exact mixture probability (eps/(n-1)); missing feedback explicit; policy version travels |
| 4. Offline comparison w/ support + uncertainty | VERIFIED LOCALLY | coverage-aware replay, CI95 per arm, insufficient-support flags, fixed-baseline compare |
| 5. Bounded exploration + drift | VERIFIED LOCALLY | durable windowed exploration budget (degrades to greedy when exhausted); drift report (recent vs long-run mean) + /usage/routing/drift + operations-page panel; security constraints OUTSIDE the reward |
| 6. Fine-tuning/multi-step RL | EXCLUDED | separate design required (per roadmap — consented data + safe environment prerequisites) |

Acceptance: compare with fixed routing (held-out comparison tests),
report verified quality/cost/latency/confidence by category
(`policy_value` by_category). Rollout: the UCB/epsilon router is a
foundation — NO weight training claimed.

## Phase 13 subphase ledger — ALL 33 SUBPHASES (3 Nov, v25.0.0)

| Subphase | Status | Evidence |
|---|---|---|
| 13.0 evidence baseline | VERIFIED LOCALLY | ledger + RELEASE_EVIDENCE with historical attribution |
| 13.1 ReAct execution evidence | VERIFIED LOCALLY | valid step trace, dispatch digest, supersede (chat1) |
| 13.2 durable evaluation delivery | VERIFIED LOCALLY | claim_one, drainer, fencing, parity (chat1) |
| 13.3 connector readiness | VERIFIED LOCALLY | passing/current/credential-bound evidence required |
| 13.4 browser validator live | VERIFIED LOCALLY | full validator + consequential classification + effect gate |
| 13.5 frontend carryover | VERIFIED LOCALLY | field alignment, states, retry (chat1-era + L fixes) |
| 13.6 learning feedback identity | VERIFIED LOCALLY | mechanism, fallback attribution, idempotent reward |
| 13.7 first supported target | VERIFIED LOCALLY | read-only prompt family end-to-end (rollout) |
| 13.8 reviewed-failure query | VERIFIED LOCALLY | terminal-only, tenant-scoped, clustered |
| 13.9 reviewable proposals | VERIFIED LOCALLY | real unified diffs, dedup |
| 13.10 immutable manifests | VERIFIED LOCALLY | pinned hashes, refs rejected, mutation invalidates |
| 13.11 dev vs held-out | VERIFIED LOCALLY | six families, draft-proof, verbatim use |
| 13.12 typed metrics | VERIFIED LOCALLY | finite/boolean/direction/server-owned constraints |
| 13.13 durable comparisons | VERIFIED LOCALLY | resumable per-case, fenced, EVALUATED advance |
| 13.14 runtime consumption | VERIFIED LOCALLY | version on the step, precedence |
| 13.15 authority boundaries | VERIFIED LOCALLY | Principal capability gates |
| 13.16 measured shadow | VERIFIED LOCALLY | recorded executions = shadow observed |
| 13.18 exposure evidence | VERIFIED LOCALLY | declared minimums gate canary/promotion |
| 13.19 effective activation | VERIFIED LOCALLY | coordinator receipt, API-driven |
| 13.20 exact-baseline rollback | VERIFIED LOCALLY | confirmed revert, newer-release protection |
| 13.21 storage/concurrency | VERIFIED LOCALLY | ownership checks, guards, target queries |
| 13.22 measured recovery | VERIFIED LOCALLY | verified-only credit, per-class rates |
| 13.23 connector revisions | VERIFIED LOCALLY | candidates unavailable until gates |
| 13.24 DOM/browser fixtures | BLOCKED | real-browser harness (§9.20) — infrastructure |
| 13.25 React experiment workspace | INTEGRATED | lifecycle UI + L fixes; journey Playwright pending |
| 13.26 reconnect resilience | VERIFIED LOCALLY | useRunEvents consumer wired (workspace) |
| 13.27 3D polish | INTEGRATED | illustrative labeling; scene foundations preserved |
| 13.28 specialist contracts | VERIFIED LOCALLY | review's specialist table via bounded contracts |
| 13.29 deliverable verification | VERIFIED LOCALLY | artifact + provenance checks |
| 13.30 flagship journey | VERIFIED LOCALLY | full vertical journey (chat9) — generation step EXCLUDED per release scope |
| 13.31 governance | VERIFIED LOCALLY | metering, redaction, retention, bounds |
| 13.32 evidence levels | VERIFIED LOCALLY | levels 1–3 recorded; level 4 excluded (staging) |
| 13.33 progressive rollout | VERIFIED LOCALLY | stage flags wired into the API |

## Phase 13 review-round status (3 Nov, v23.0.0)

| Requirement | Status |
|---|---|
| §13.1 clusters → candidates with hypothesis + expected benefit | VERIFIED LOCALLY (proposer pipeline; drafting step injectable, nothing fabricated without it) |
| §13.2 baseline/candidate immutable, same held-out set | VERIFIED LOCALLY (pinned hashes; real prompt VERSIONS now back the refs) |
| §13.3 shadow → bounded canary, predeclared thresholds | VERIFIED LOCALLY (deterministic cohorts; shadow never serves the candidate; fraction honored) |
| §13.4 separated permissions; agents cannot grade/promote | VERIFIED LOCALLY (agent-proposed candidates refused at promotion) |
| §13.5 reviewable diffs + versioned rollback | VERIFIED LOCALLY (candidate diffs; rollback restores the PRIOR prompt version / policy snapshot end to end) |
| Rollout: prompts for read-only tasks first | INTEGRATED (engine resolves `_prompt_id` tasks through the store; fail-open) |
| Remaining | BLOCKED: the LLM drafting step for candidate templates (needs a wired model call + reviewed dataset), staging evidence |

## Supported scope (12.1)

Journeys with concrete fixtures and expected outcomes:

1. **Existing connector completes an authorized task** — fixture: core/tool router suites; outcome: verified task + effect CONFIRMED_APPLIED.
2. **Missing connector built from supplied API info** — fixture: `test_connector_build_service.py` + `test_review_batch2_build_contracts.py`; outcome: durable command → build → validated operations → activated revision.
3. **Authentication required after generation** — fixture: readiness gate (`test_missing_credentials_preserve_candidate_with_next_action`); outcome: candidate preserved, precise next action, no rebuild.
4. **Parent resumes after required-operation validation** — fixture: `evaluate_capability_readiness` ladder; outcome: resume only at ACTIVATED.
5. **Temporary failure recovered** — fixture: `test_recovery_worker_acceptance.py`; outcome: one logical recovery, verified before success claims.
6. **Uncertain write pauses for reconciliation** — fixture: effect suites (§5); outcome: OUTCOME_UNKNOWN blocks verified success until evidence resolves.
7. **Real artifact delivered and independently checked** — fixture: verifier artifact criterion with resolver; outcome: unresolved artifact can never pass.

Explicitly EXCLUDED from the current release claim: real-browser DOM journeys (§9.20 harness), standalone-server runtime proof (§7.5), same-session takeover gateway (§8.15), self-extension benchmark (12.21). These stay BLOCKED until their infrastructure exists.

## Phase 19 Batch A register (6 Nov, v41.0.0 — C1–C10 closure)

| Requirement | Status | Named evidence |
|---|---|---|
| C1 Firestore deliverable parity | VERIFIED LOCALLY | test_phase19_baseline.py::test_c1 + review/publication contracts on both stores |
| C2 per-file verification | VERIFIED LOCALLY | test_phase19_baseline.py::test_c2 (multi-file pass, subset fail, empty fail) |
| C3 truthful completion | VERIFIED LOCALLY | test_phase19_baseline.py::test_c3 (no emit → no completion claim); recovery emits |
| C4 immutable atomic review | VERIFIED LOCALLY | test_phase19_baseline.py::test_c4 (confirm runs the gate) |
| C5 metering before dispatch | VERIFIED LOCALLY | test_phase18_batch_a.py (3 attributed reservations; denied = zero dispatches) |
| C6 deadline across auth/CSRF | INTEGRATED | ensureCsrf shares the deadline + cancellation |
| C7 generation production callers | VERIFIED LOCALLY | test_phase19_baseline.py::test_c7 (login/signup/guest/logout) |
| C8 scoped run events | VERIFIED LOCALLY | test_phase19_baseline.py::test_c8 + frontend/tests/phase19-design.test.ts |
| C9 draft identity/submission | VERIFIED LOCALLY | test_phase19_baseline.py::test_c9 |
| C10 honest completion claims | INTEGRATED | ledger statuses; BLOCKED items remain named |
| 19.15 scene interaction | VERIFIED LOCALLY | frontend/tests/phase19-design.test.ts (no touch interception) |
| 19.18 graph a11y/state | VERIFIED LOCALLY | frontend/tests/phase19-design.test.ts (buttons; identity selection) |

## Phase 19 Batch A register (6 Nov, v41.0.0 — C1–C10 closure)

| Requirement | Status | Named evidence |
|---|---|---|
| C1 Firestore deliverable parity | VERIFIED LOCALLY | test_phase19_baseline.py::test_c1 + review/publication contracts on both stores |
| C2 per-file verification | VERIFIED LOCALLY | test_phase19_baseline.py::test_c2 (multi-file pass, subset fail, empty fail) |
| C3 truthful completion | VERIFIED LOCALLY | test_phase19_baseline.py::test_c3 (no emit → no completion claim); recovery emits |
| C4 immutable atomic review | VERIFIED LOCALLY | test_phase19_baseline.py::test_c4 (confirm runs the gate) |
| C5 metering before dispatch | VERIFIED LOCALLY | test_phase18_batch_a.py (3 attributed reservations; denied = zero dispatches) |
| C6 deadline across auth/CSRF | INTEGRATED | ensureCsrf shares the deadline + cancellation |
| C7 generation production callers | VERIFIED LOCALLY | test_phase19_baseline.py::test_c7 (login/signup/guest/logout) |
| C8 scoped run events | VERIFIED LOCALLY | test_phase19_baseline.py::test_c8 + frontend/tests/phase19-design.test.ts |
| C9 draft identity/submission | VERIFIED LOCALLY | test_phase19_baseline.py::test_c9 |
| C10 honest completion claims | INTEGRATED | ledger statuses; BLOCKED items remain named |
| 19.15 scene interaction | VERIFIED LOCALLY | frontend/tests/phase19-design.test.ts (no touch interception) |
| 19.18 graph a11y/state | VERIFIED LOCALLY | frontend/tests/phase19-design.test.ts (buttons; identity selection) |

## Phase 18 register (6 Nov, v40.0.0 — React reliability + carryover closure)

| Requirement | Status | Named evidence |
|---|---|---|
| 18.0 baseline (C1–C14 + F1–F10) | VERIFIED LOCALLY | test_phase18_baseline.py |
| 18.1 deliverable startup/storage | VERIFIED LOCALLY | test_phase18_batch_a.py (pagination/readiness; factory init from v39.1) |
| 18.2 connected artifact lifecycle | INTEGRATED | create_files → record_and_publish → staged publication; publication_jobs durable |
| 18.3 consolidated verification | VERIFIED LOCALLY | typed verdict contract (objects+dicts; C5/C6 closed v39.1) |
| 18.4 reviews change content | VERIFIED LOCALLY | test_phase18_batch_a.py (atomic revision; content mutation) |
| 18.5 rendered verification | VERIFIED LOCALLY | extracted-text check (comment-only fails; C11 closed) |
| 18.6 cleanup + invalidation | VERIFIED LOCALLY | test_phase18_batch_a.py (worker completes registered dependents) |
| 18.7 per-invocation accounting | VERIFIED LOCALLY | metered_model wrapper; 3-invocation identity test |
| 18.8 state contracts | VERIFIED LOCALLY | frontend/tests/phase18-reliability.test.ts |
| 18.9 request layer | VERIFIED LOCALLY | request-policy.ts (combined signals; aborted never reaches transport) |
| 18.10 command identity | INTEGRATED | ChatComposer ambiguous-acceptance flow (same idempotency key) |
| 18.11 auth state machine | INTEGRATED | auth-state.ts (generation identifier) |
| 18.12 cross-tab refresh | VERIFIED LOCALLY | refresh-lock.ts (F9: non-owners re-read, never re-rotate) |
| 18.13 session architecture | INTEGRATED | docs/session-and-refresh.md (token client retained, documented) |
| 18.14 subscription owner | INTEGRATED | run-subscriptions.ts (reference-counted; coordinated poller) |
| 18.15 cursor/opaque IDs | VERIFIED LOCALLY | integer conversion removed (workflows.py) |
| 18.16 stream handling | INTEGRATED | bounded dedup (2000 FIFO); silent-error sanitization |
| 18.17 connectivity vs capability | VERIFIED LOCALLY | useNetworkStatus online hint; wired into useRunEvents |
| 18.18 polling races | VERIFIED LOCALLY | useCommandStatus current-result loop |
| 18.19 draft restoration (F1) | VERIFIED LOCALLY | draft-store.ts + ChatComposer adoption |
| 18.21 evidence panel guards (F10) | VERIFIED LOCALLY | generation guards; review errors; stale display |
| Subscription ownership at call sites | VERIFIED LOCALLY | workspace page + useRunEvents consume the shared manager |
| Correction → deliverable staleness | VERIFIED LOCALLY | test_phase18_batch_a.py::test_correction_marks_deliverable_verification_stale |
| Coroutine/unknown-object verifier probe | VERIFIED LOCALLY | test_phase18_batch_a.py::test_coroutine_and_unknown_objects_never_pass |
| Evidence panel: resume + workflow views | INTEGRATED | resume page (v39.1) + workflow page deep-link (?deliverable=...) |
| Real-backend browser journeys | BLOCKED | requires a deployed frontend+backend pair (unchanged exclusion) |
| Firestore emulator acceptance | BLOCKED | requires the emulator environment (unchanged exclusion) |

## Phase 17 review-gap closure (5 Nov, v39.1.0)

| Gap | Status | Evidence |
|---|---|---|
| Deliverable repo not factory-initialized | CLOSED | test_phase17_review_gaps.py::test_deliverable_repo_is_factory_initialized (both stores) |
| Publisher/verifier without production callers | CLOSED | test_phase17_review_gaps.py (create_files → delivery entry; resume route records) |
| Evidence panel unmounted | CLOSED | test_phase17_review_gaps.py::test_evidence_panel_is_mounted_on_the_resume_page |
| Copied verdict logic in tests | CLOSED | shared artifactVerdictStyle; vitest imports the lib |
| Probe: typed failure OBJECT | CLOSED | test_phase17_review_gaps.py::test_typed_failure_object_fails_not_bool_passes |
| Probe: checksum-only, no byte check | CLOSED | test_phase17_review_gaps.py::test_checksum_without_byte_check_is_needs_review |
| Probe: comment-only text | CLOSED | test_phase17_review_gaps.py::test_comment_only_text_fails_rendered_verification |

## Phase 17 register (5 Nov, v39.0.0 — verified deliverables + carryover closure)

Findings A–H registered at §17.0 (test_phase17_baseline.py) and closed
through production paths; the full register follows.

| Requirement | Status | Named evidence |
|---|---|---|
| 17.0 acceptance baseline | VERIFIED LOCALLY | test_phase17_baseline.py (findings registered w/ owner+case) |
| 17.1 supported deliverables | VERIFIED LOCALLY | backend/models/deliverable.py (narrow set + not_yet_verified per type) |
| 17.2 paid-execution coverage (D) | VERIFIED LOCALLY | test_phase17_batch_a.py (3-call fixture; accumulating usage; metered embedding) |
| 17.3 memory service everywhere (A) | VERIFIED LOCALLY | memory_service.py + orchestrator tools; test_phase17_batch_b.py |
| 17.4 durable memory ops (B, C) | VERIFIED LOCALLY | test_phase17_batch_b.py (versions, concurrency, restart cleanup, behavioral parity) |
| 17.5 connector binding context | VERIFIED LOCALLY | AuthMetadata §17.5 fields; rotation test (batch A) |
| 17.6 deliverable contract (G) | VERIFIED LOCALLY | test_phase17_batch_c.py (identity/lineage/verification readable) |
| 17.7 staged publication | VERIFIED LOCALLY | test_phase17_batch_c.py (5 crash windows; none fake readiness) |
| 17.8 manifest integrity | VERIFIED LOCALLY | test_phase17_batch_c.py (escaping/reserved/entrypoint/bytes) |
| 17.9 typed verification (E) | VERIFIED LOCALLY | artifact_verifier.py + test_phase17_batch_d.py (negative dict fails) |
| 17.11 field provenance (E) | VERIFIED LOCALLY | test_phase17_batch_d.py (10%→90%; changed employer; self-declared) |
| 17.13 reverification after edits (F) | VERIFIED LOCALLY | test_phase17_batch_d.py (tailoring recomputes; fail-closed) |
| 17.14 rendered validation | VERIFIED LOCALLY | test_phase17_batch_e.py (parses/text/sections/links/vectors) |
| 17.15 preview isolation | VERIFIED LOCALLY | test_phase17_batch_e.py (documents sandbox=""; apps no same-origin) |
| 17.16 transparent ATS | VERIFIED LOCALLY | test_phase17_batch_e.py (rubric version/weights/disclaimers) |
| 17.17 honest project states | VERIFIED LOCALLY | test_phase17_batch_e.py (unexecuted → no badges) |
| 17.18 bounded repair | VERIFIED LOCALLY | test_phase17_batch_f.py (clarification; escalation) |
| 17.19 deliverable specialists | VERIFIED LOCALLY | test_phase17_batch_f.py (boundaries; read-only reviewer) |
| 17.20 verified-only rewards | VERIFIED LOCALLY | test_phase17_batch_f.py (byte-bound eligibility; versioned corrections) |
| 17.21 evidence panel | VERIFIED LOCALLY | ArtifactEvidencePanel.tsx + /deliverables API |
| 17.22 revision-bound review | VERIFIED LOCALLY | 409 REVISION_MISMATCH (deliverables.py) |
| 17.24 source propagation | VERIFIED LOCALLY | test_phase17_batch_h.py (stale provenance, history intact) |
| 17.25 regression matrix (H) | VERIFIED LOCALLY | test_phase17_batch_h.py (AUTHORIZED nonempty fixtures) |
| 17.26 complete journey | VERIFIED LOCALLY | test_phase17_batch_h.py (16 steps) |
| 17.27 documentation | INTEGRATED | deliverable-verification.md; document-provenance.md; runbooks/deliverable-failure.md |
| 17.10 engine integration | INTEGRATED | artifact_verifier resolves via the owner-scoped repo; engine state mapping follows §17.10's distinct states through TaskStatus/evaluation (existing) |
| Staging evidence | BLOCKED | requires a real deployment (unchanged exclusion) |
| Managed runner | EXCLUDED | §7 — generated code stays unexecuted/labeled |

## Phase 16 findings A–K (5 Nov, v38.0.0 — governance at the boundary)

The completeness census: 13/13 Phase 16 services had ZERO production
callers. All wired through the memory API/worker/delegation:

| Finding | Status | Named evidence |
|---|---|---|
| A router bypassed governance | CLOSED | test_phase16_findings.py (add/search/list governed at the HTTP boundary) |
| B hostile flags missing on results | CLOSED | test_phase16_findings.py::test_hostile_content_is_flagged_not_obeyed |
| C forget was an immediate delete | CLOSED | barrier → /cleanup (test_phase16_findings.py) |
| D corrections didn't propagate | CLOSED | dependency edges registered at the API layer |
| E episodes never recorded | CLOSED | worker records episodes from evaluated runs |
| F procedures never proposed | CLOSED | verified episodes → candidates (needs confirmation) |
| G specialist scope not carried | CLOSED | delegated contracts carry non-widening memory_scope |
| H export lacked the secret gate | CLOSED | the export withholds on detected secrets |
| I dependency map was test-only | CLOSED | process-level map at the API layer |
| J stage flags unwired | CLOSED | MEMORY_STAGES_ENABLED in settings |
| K effect evaluation had no surface | CLOSED | /memory/effect (admin; abstains when unmeasured) |

## Phase 16 subphase register (5 Nov, v37.0.0 — governed memory)

| Subphase | Status | Named evidence |
|---|---|---|
| 16.0 memory inventory | VERIFIED LOCALLY | test_phase16_governance.py (every path named) |
| 16.1 explicit scopes | VERIFIED LOCALLY | test_phase16_governance.py (org membership exposes nothing; shared refs only) |
| 16.2 record contract | VERIFIED LOCALLY | test_phase16_governance.py (source/owner/status traceable) |
| 16.3 write policy | VERIFIED LOCALLY | test_phase16_governance.py (explainable; explicit ≠ inferred) |
| 16.4 secrets excluded | VERIFIED LOCALLY | test_phase16_governance.py (refused pre-persistence) |
| 16.5 durable idempotent writes | VERIFIED LOCALLY | test_phase16_governance.py (replay returns the SAME memory) |
| 16.6 candidate validation | VERIFIED LOCALLY | test_phase16_governance.py (server decides; no self-confirmation) |
| 16.7 dedup preserves differences | VERIFIED LOCALLY | test_phase16_governance.py (per-user separation) |
| 16.8 contradictions/supersession | VERIFIED LOCALLY | test_phase16_governance.py (coexistence + correction history) |
| 16.9 freshness | VERIFIED LOCALLY | test_phase16_governance.py (expired ≠ current) |
| 16.10 authorization before retrieval | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (unauthorized never results) |
| 16.11 retrieval methods | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (exact beats vague) |
| 16.12 separate ranking signals | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (explainable) |
| 16.13 bounded context | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (statuses verbatim) |
| 16.14 content as data | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (hostile content inert) |
| 16.15 specialist memory scope | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (non-widening filters) |
| 16.16 execution memory | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (honest outcomes) |
| 16.17 procedures | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (reviewer gate) |
| 16.18 revalidation before reuse | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (current gates) |
| 16.19 preference precedence | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (task overrides) |
| 16.20 grounded facts | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (drafts stay inferred) |
| 16.21 correction propagation | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (dependents invalidated) |
| 16.22 deletion workflow | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (barrier + cleanup) |
| 16.23 export | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (provenance, no secrets) |
| 16.24 index migrations | VERIFIED LOCALLY | test_phase16_retrieval_workflows.py (deliberate switch) |
| 16.25 retrieval quality/abstention | VERIFIED LOCALLY | test_phase16_quality_release.py (the matrix) |
| 16.26 measured memory effect | VERIFIED LOCALLY | test_phase16_quality_release.py (with/without) |
| 16.27 memory workspace UI | VERIFIED LOCALLY | dashboard/memory/page.tsx (status/source/scope direct) |
| 16.28 UI state reliability | VERIFIED LOCALLY | frontend/tests/phase16-memory.test.ts |
| 16.29 relationship view | VERIFIED LOCALLY | real supersession edges; motion-gated |
| 16.30 storage parity | VERIFIED LOCALLY | test_phase16_quality_release.py (surface parity) |
| 16.31 flagship journey | VERIFIED LOCALLY | test_phase16_quality_release.py (12 steps) |
| 16.32 progressive release | VERIFIED LOCALLY | test_phase16_quality_release.py (independent flags) |

## Phases 14–15 closure audit W1–W8 (4 Nov, v36.0.0)

| Wiring | Status | Named evidence |
|---|---|---|
| W1 release-stage gate inside delegate() | CLOSED | test_phase15_production_wiring.py (refuses disabled roles; honest default stages) |
| W2 delegation decision recorded | CLOSED | engine verification block records reason + alternatives |
| W3 recovery classifies before responding | CLOSED | recovery_worker episode carries the permitted response |
| W4 fact-grounded resume building | CLOSED | resume_builder runs check_resume_facts; additions flagged |
| W6 typed handoff + browser session contract | CLOSED | verifier returns the handoff; web_agent validates + returns the contract |
| W7 user-category silent-drop (REAL BUG) | CLOSED | test_phase15_production_wiring.py (coercion; strict schema preserved for programmatic callers) |
| W8 checkpoint reader | CLOSED | test_phase15_production_wiring.py (roundtrip) |

## Phase 15 findings A–K (4 Nov, v35.0.0 — the specialist system audited)

| Finding | Status | Named evidence |
|---|---|---|
| A dead §15.5 recheck | CLOSED | test_phase15_findings.py (default provider consulted; startup wiring in main.py) |
| B unconsumed completion verdicts | CLOSED | test_phase15_coordination.py (boundary gate) |
| D unattributed specialist attempts | CLOSED | engine delegation records the durable decision; test_phase15_findings.py |
| E orphaned specialization evaluator | CLOSED | /experiments/specialization/compare (admin) |
| F orphaned learned selection | CLOSED | test_phase15_findings.py (stage-scoped decision logging) |
| G stale workspace panel | CLOSED | SpecialistPanel revision prop + vitest |
| H unfenced Firestore transitions | CLOSED | ownership fence parity; test_phase15_release.py surface |
| I deadline-expired RUNNING stuck | CLOSED | test_phase15_findings.py (honest TIMED_OUT sweep; worker-driven) |
| J broken RUNNING takeover (real bug) | CLOSED | test_phase15_findings.py (reclaim queue + takeover) |
| K deliverable access control | VERIFIED | test_phase15_findings.py (§12.28 ownership) |
| Context schema gap (via D) | CLOSED | specialist_delegation/verification are legitimate buckets |

## Phase 15 subphase register (4 Nov, v34.0.0 — every row named + tested)

| Subphase | Status | Named evidence |
|---|---|---|
| 15.0 delegation baseline/inventory | VERIFIED LOCALLY | test_phase15_foundation.py (execution paths queryable; placeholders explicit) |
| 15.1 when to delegate | VERIFIED LOCALLY | test_phase15_foundation.py (deterministic policy; reasons + alternatives recorded) |
| 15.2 one task hierarchy | VERIFIED LOCALLY | test_phase15_foundation.py (subtree_of reconstruction from persistence) |
| 15.3 complete contracts | VERIFIED LOCALLY | test_phase15_foundation.py (refuses incomplete; full field set) |
| 15.4 authority intersection | VERIFIED LOCALLY | test_phase15_foundation.py (prompt grants nothing; approval gates) |
| 15.5 recheck at execution/resume | VERIFIED LOCALLY | test_phase15_foundation.py (typed AuthorizationLost; runners wired) |
| 15.6 bounded tree | VERIFIED LOCALLY | test_phase15_coordination.py (depth/cycles/equivalence/caps) |
| 15.7 budgets | VERIFIED LOCALLY | test_phase15_coordination.py (reservation bound; per-attempt spend) |
| 15.8 atomic dispatch | VERIFIED LOCALLY | test_phase15_coordination.py (dedup identity; pending_contracts drain) |
| 15.9 fenced ownership | VERIFIED LOCALLY | test_phase15_coordination.py (terminal transitions fenced on the claim) |
| 15.10 dependency scheduling | VERIFIED LOCALLY | test_phase15_coordination.py (a stopped process ≠ a success) |
| 15.11 shared resources | VERIFIED LOCALLY | test_phase15_coordination.py (one controller; artifact identity) |
| 15.12 structured handoffs | VERIFIED LOCALLY | test_phase15_handoffs.py (content classes; embedded instructions are data) |
| 15.13 boundary validation | VERIFIED LOCALLY | test_phase15_handoffs.py (schema-enforced outputs) |
| 15.14 independent verification | VERIFIED LOCALLY | test_phase15_handoffs.py (frozen criteria; judge cannot upgrade a fail) |
| 15.15 partial completion | VERIFIED LOCALLY | test_phase15_handoffs.py (per-requirement states; uncertain ≠ verified) |
| 15.16 cancellation | VERIFIED LOCALLY | test_phase15_handoffs.py (receipts preserved; cancelled parent admits nothing) |
| 15.17 recovery classification | VERIFIED LOCALLY | test_phase15_handoffs.py (class table; reconcile-first) |
| 15.18 builder chain | VERIFIED LOCALLY | test_phase15_specialist_paths.py (generated ≠ available) |
| 15.19 validator operational | VERIFIED LOCALLY | test_phase15_specialist_paths.py (full case set; exact-scope records) |
| 15.20 browser contracts | VERIFIED LOCALLY | test_phase15_specialist_paths.py (stale targets; click ≠ completion) |
| 15.21 grounded deliverables | VERIFIED LOCALLY | test_phase15_specialist_paths.py (additions flagged; claims classified) |
| 15.22 attribution | VERIFIED LOCALLY | test_phase15_learning.py (per-attempt credit; human resolution) |
| 15.23 specialization evaluation | VERIFIED LOCALLY | test_phase15_learning.py (measured cases; unmeasured ≠ zero) |
| 15.24 learned selection | VERIFIED LOCALLY | test_phase15_learning.py (contextual policy; selection ≠ authority) |
| 15.25 workspace UI | VERIFIED LOCALLY | /workflows/{id}/delegations + cancel; SpecialistPanel + distinct interventions |
| 15.26 React reliability | VERIFIED LOCALLY | frontend/tests/phase15-coordination.test.ts (authoritative counters) |
| 15.27 3D coordination view | VERIFIED LOCALLY | CoordinationGraph (server-backed, motion-gated, 2D always available) |
| 15.28 storage parity | VERIFIED LOCALLY | test_phase15_release.py (surface parity + full scenario run) |
| 15.29 flagship journey | VERIFIED LOCALLY | test_phase15_release.py (discovery → verification, no success flags) |
| 15.30 progressive release | VERIFIED LOCALLY | test_phase15_release.py (stages; operator disable; role versions) |

Carryover from the Phase 14 acceptance register (lease fencing, durable
evaluations, attribution, budgets, required operations, browser
ownership, pinned comparisons, durable activation, storage parity,
honest UI states) — all VERIFIED LOCALLY through the Phase 14 register
(v33) and exercised inside the Phase 15 scenarios above. EXCLUDED
(unchanged): staging drills, real-browser fixtures, the self-extension
benchmark on a live deployment, SSO/marketplace.

## Phase 14 subphase register (4 Nov, v33.0.0 — every row named + tested)

Supersedes the v32 block below where they differ; A–K rows unchanged.

| Subphase | Status | Named evidence |
|---|---|---|
| 14.0 evidence register | VERIFIED LOCALLY | test_phase14_foundation.py (refuses unevidenced claims) |
| 14.1 release scope | VERIFIED LOCALLY | test_phase14_foundation.py::test_contextual_selector_is_release_scoped |
| 14.2 per-category measurements | VERIFIED LOCALLY | test_phase14_foundation.py (quality/CI/cost/latency) + /usage/routing/summary |
| 14.3 job recovery | VERIFIED LOCALLY | test_phase14_job_recovery.py (5 windows) |
| 14.4 capability gates | VERIFIED LOCALLY | test_phase14_capability_gate.py (ladder + §14.H composition) |
| 14.5 browser state checkpoints | VERIFIED LOCALLY | test_phase14_browser_checkpoint.py (durable, monotonic) |
| 14.6 comparison pipeline | VERIFIED LOCALLY | test_phase14_comparison_pipeline.py (4 contracts) |
| 14.7 activation crash-safety | VERIFIED LOCALLY | test_phase14_receipts.py |
| 14.8 learning ownership | VERIFIED LOCALLY | test_phase14_learning_ownership.py (AST guard) |
| 14.9 action catalog | VERIFIED LOCALLY | test_phase14_foundation.py (visible availability) |
| 14.10 eligibility filter | VERIFIED LOCALLY | test_phase14_foundation.py (pinned refusal; one source) |
| 14.11 context schema | VERIFIED LOCALLY | test_phase14_foundation.py (validation at persistence) |
| 14.12 decision record | VERIFIED LOCALLY | test_phase14_foundation.py (full-field persistence) |
| 14.13 one selector | VERIFIED LOCALLY | test_phase14_foundation.py (AST: one site) |
| 14.14 correct probabilities | VERIFIED LOCALLY | test_phase14_parity_and_rewards.py (sums to 1) |
| 14.15 atomic exploration | VERIFIED LOCALLY | test_phase14_parity_and_rewards.py (hard cap) |
| 14.16 fallback attribution | VERIFIED LOCALLY | test_phase14_rewards.py::test_fallback_lineage_holds_through_reward_events |
| 14.17 reward specification | VERIFIED LOCALLY | test_phase14_parity_and_rewards.py (single source) |
| 14.18 durable rewards | VERIFIED LOCALLY | test_phase14_rewards.py |
| 14.19 storage parity | VERIFIED LOCALLY | test_phase14_parity_and_rewards.py (3 repo surfaces) |
| 14.20 cost/latency | VERIFIED LOCALLY | test_phase14_rewards.py (cost EWMA; never a bonus) |
| 14.21 quarantine | VERIFIED LOCALLY | test_phase14_rewards.py (malformed events) |
| 14.22 supported estimator | VERIFIED LOCALLY | test_phase14_rewards.py (IPS vs descriptive) |
| 14.23 baselines | VERIFIED LOCALLY | test_phase14_eval_governance.py (fixed baseline by category) |
| 14.24 uncertainty | VERIFIED LOCALLY | test_phase14_eval_governance.py (CI95 on means + IPS) |
| 14.25 policy artifacts | VERIFIED LOCALLY | test_phase14_eval_governance.py (exact restore) |
| 14.26 shadow execution | VERIFIED LOCALLY | test_phase14_rollout_gates.py (measured observations) |
| 14.27 bounded canary | VERIFIED LOCALLY | test_phase14_rollout_gates.py (hard cap + fraction) |
| 14.28 deployment adapter | VERIFIED LOCALLY | test_phase14_rollout_gates.py (deterministic variants; provenance) |
| 14.29 drift windows | VERIFIED LOCALLY | test_phase14_rollout_gates.py + test_routing_policy.py (count-rotated) |
| 14.30 recovery strategies | VERIFIED LOCALLY | test_phase14_strategies.py (verified-only credit) |
| 14.31 tool discovery | VERIFIED LOCALLY | test_phase14_strategies.py (ordered ladder) |
| 14.32 browser strategies | VERIFIED LOCALLY | test_phase14_strategies.py (honest levels) |
| 14.33 specialist measurement | VERIFIED LOCALLY | test_phase14_strategies.py (measurable contracts) |
| 14.34 learning dashboard | VERIFIED LOCALLY | operations page (health + drift + leaderboard); tsc clean |
| 14.35 React reliability | VERIFIED LOCALLY | frontend/tests/phase14-reliability.test.ts |
| 14.36 3D polish | VERIFIED LOCALLY | tested zoom clamp (wheel + pinch, one contract) |
| 14.37 flagship journey | VERIFIED LOCALLY | test_phase14_flagship.py (complete vertical) |
| 14.38 release evidence | VERIFIED LOCALLY | test_phase14_release_evidence.py + RELEASE_EVIDENCE.md packet |
| 14.39 RL research lane | VERIFIED LOCALLY | test_rl_research_lane.py (AST guard) |

## Phase 14 — verified contextual routing + controlled learning (4 Nov, v32.0.0)

All 39 subphases (14.0–14.39) plus audit findings A–K. Evidence levels:
VERIFIED LOCALLY = acceptance test in the suite; INTEGRATED = wired into
the runtime path.

| Requirement | Status | Evidence |
|---|---|---|
| 14.A selection probabilities describe the COMPLETE policy | VERIFIED LOCALLY | P(top)=1−ε=0.90, P(other)=ε/(n−1); `test_routing_policy.py::test_exploration_logs_exact_probability` |
| 14.B atomic exploration admission | VERIFIED LOCALLY | single count-and-claim transaction (SQLite `admit_exploration`; Firestore transaction); storage failure → baseline (`test_routing_policy.py`) |
| 14.C idempotent reward consumption | VERIFIED LOCALLY | stats update + consumption mark in ONE transaction; double-apply refused by CAS (`test_phase14_rewards.py`) |
| 14.D durable reward events, independent learners | VERIFIED LOCALLY | identity (decision, evaluation, reward_version); global + contextual consume once each (`test_phase14_rewards.py`) |
| 14.E/14.22 supported estimator + descriptive-only | VERIFIED LOCALLY | IPS agreement term with degeneracy flags; descriptive replay namespaced; estimator metadata in the API (`test_phase14_rewards.py::test_offline_comparison_names_its_estimator`) |
| 14.F Firestore learning contract | VERIFIED LOCALLY (parity by construction) | contextual stats, transactional exploration, drift windows, atomic consumption (`backend/repositories/firestore/sidecar.py`) |
| 14.G evaluation-job fencing, mandatory owner | VERIFIED LOCALLY | complete/fail REQUIRE owner; owner+lease+version CAS; reconciler requeues only ownerless work (`test_phase14_job_fencing.py`) |
| 14.H required-operation propagation (undeclared ≠ empty) | VERIFIED LOCALLY | orchestrator declaration → build row + task input + command payload; readiness refuses task-originated empty declarations (`test_phase14_operation_propagation.py`) |
| 14.I browser validation from authoritative state | VERIFIED LOCALLY | runtime-captured `browser_evidence`; narrative-only claims are UNVERIFIABLE (`test_phase14_browser_authority.py`) |
| 14.J comparison dataset-hash gate + crash-safe activation | VERIFIED LOCALLY | cases must hash to the pinned dataset (409 otherwise); DURABLE activation receipts, reconciled retries, effective receipts idempotent (`test_phase14_receipts.py`) |
| 14.K frontend activation receipt consumption | VERIFIED LOCALLY | experiments page renders effective/failed/reconciled + version; tsc clean (`frontend/src/app/dashboard/experiments/page.tsx`) |
| 14.9–14.14 decision record + one selector + honest probabilities | INTEGRATED | engine logs mechanism + probability via `record_model_selection`; one contextual selector |
| 14.15 atomic exploration | VERIFIED LOCALLY | see 14.B |
| 14.16 fallback attribution | VERIFIED LOCALLY | resolved row earns the reward; failed initial model never credited through events (`test_phase14_rewards.py::test_fallback_lineage_holds_through_reward_events`) |
| 14.17 reward specification | VERIFIED LOCALLY | RewardClass mapping; unverified completions never positive (`test_routing_policy.py`) |
| 14.18 durable rewards | VERIFIED LOCALLY | see 14.C/14.D |
| 14.19 storage parity | VERIFIED LOCALLY | SQLite + Firestore learning contracts |
| 14.20 cost/latency measurement | VERIFIED LOCALLY | reward events carry cost; EWMA cost (seeded, not self-blended) scores as a tie-breaker, only from measured cost (`test_phase14_rewards.py`) |
| 14.21 data quarantine | VERIFIED LOCALLY | malformed events quarantined with visible reason, never applied (`test_phase14_rewards.py::test_malformed_reward_event_is_quarantined`) |
| 14.23/14.24 baselines + uncertainty | VERIFIED LOCALLY | fixed_policy baseline; CI95 + sufficient_support per arm |
| 14.25 policy artifacts | INTEGRATED | PolicySnapshotStore + prompt versions (Phase 13) |
| 14.26–14.29 shadow/canary/adapter/drift | VERIFIED LOCALLY | measured shadow observations, predeclared cohorts, deployment coordinator, drift windows (both stores) |
| 14.30–14.33 recovery/tool/browser/specialist measurement | INTEGRATED | recovery metrics, capability ladder, authoritative browser evidence, specialist contracts |
| 14.34 learning dashboard | VERIFIED LOCALLY | routing-log health panel + drift table + leaderboard (operations page) |
| 14.37 flagship journey | VERIFIED LOCALLY | chat9 evidence levels 1–3 |
| 14.38 release evidence | INTEGRATED | release_verifier specialist assembles candidate evidence |
| 14.39 RL research lane | VERIFIED LOCALLY | guard test: the execution path imports no research-lane module; learning writes flow only through the reward contract (`test_rl_research_lane.py`) |

Remaining EXCLUDED (unchanged from v31): staging drills, real-browser
fixtures, the flagship benchmark, SSO/marketplace.

## Phase 27 — integration and production qualification (12 Nov, v6.3.0)

The authoritative Phase 27 register (defect closure 27.D1–D10, inherited
27.R1–R12, per-outcome owner/evidence rows, 26.P1–P4, and the frozen
18-stage / 24-journey denominators) lives at
[qualification/phase-27-plan.md](qualification/phase-27-plan.md); this
ledger does not duplicate it. Summary of implementation states:

| Package | Status | Evidence |
|---|---|---|
| 27.0 register | IMPLEMENTED | per-outcome rows with owner, evidence state, candidate/receipt columns |
| 27.1 qualification gate | IMPLEMENTED | strict qualifier; producer/parser receipt contract; schema regressions |
| 27.2 identity/budgets/effects | VERIFIED LOCALLY | adversarial suites (security stage) |
| 27.3 runner + egress broker | IMPLEMENTED | port-specific grants; broker test matrix; execution store |
| 27.4 WebAgent/browser unification | IMPLEMENTED | generation fencing, port-aware origins, deployed takeover journey |
| 27.5 agent builds its tool | IMPLEMENTED | real adapter + standalone MCP journeys (flagship stage) |
| 27.6 marketplace | IMPLEMENTED | J23 journey (install → credential → qualified operation) |
| 27.7 workflows/ReAct | VERIFIED LOCALLY | engine suites + flagship |
| 27.8 every enabled agent | IMPLEMENTED | typed runners + real-path journeys for ALL enabled roles (`test_phase27_agent_roles_journey.py`) |
| 27.9 self-healing | VERIFIED LOCALLY | fault-injection stage |
| 27.10 RL/evolution | IMPLEMENTED | observation-derived baseline; governed rollout worker journey |
| 27.11 usage accuracy | IMPLEMENTED | per-model limits; ledger-backed token card |
| 27.12 storage/memory/feedback | IMPLEMENTED | bounded retention sweep; memory lifecycle; feedback parity |
| 27.13 deliverables | VERIFIED LOCALLY | provenance + rendered verification suites |
| 27.14 live workspace | IMPLEMENTED | online-reconnect reconciliation (vitest) |
| 27.15 3D + modeling | IMPLEMENTED | modeling service + Studio wiring + J24 journey |
| 27.16 hosted providers | BLOCKED (B1) | no credentials — release owner |
| 27.17 cloud deployments | BLOCKED | no cloud targets — release owner |
| 27.18 independent restore | IMPLEMENTED | full app restore drill (14 checks) wired into s_restore |
| 27.19 workload measurement | IMPLEMENTED | whole-workload producer with predeclared gates |
| 27.20 pilot + journeys | BLOCKED (B4) for pilot; journeys J01–J24 consumed by the qualifier |
| 27.21 final qualification | EXECUTED per attempt | verdicts recorded per target in the register |

## Phase 28 — complete workspace journey (12 Nov, v6.4.0)

The authoritative Phase 28 register (batches 28.1–28.12, inherited
27.D1–D10 / 27.R1–R12 / 26.P1–P4, per-stage evidence contracts, and the
scope reconciliation of older marketplace exclusions) lives at
[qualification/phase-28-plan.md](qualification/phase-28-plan.md); this
ledger does not duplicate it. Original IDs are preserved unchanged.

| Batch | Status | Evidence |
|---|---|---|
| 28.1 evidence gate | IMPLEMENTED | stage-bound JUnit names; 3–5-participant pilot contract; linked-trace content checks; named regressions (test_phase28_evidence_gate.py, 14 tests + updated 25/26/27 fixtures) |
| 28.2 execution controls | VERIFIED LOCALLY | SQLite journal legacy migration; durable-cancel/unreachable-owner/mid-run-revocation/broker-decision drills (test_phase28_execution_controls.py, 6 tests) |
| 28.3 browser/DOM | VERIFIED LOCALLY | per-node DOM identity token (duplicate-label decoy refused on real Chromium); iframe-target + port drills (test_phase28_browser_drills.py, 3 tests) |
| 28.4 self-building MCP | VERIFIED LOCALLY | worker-killed-during-generation + killed-between-validation-and-activation drills on PostgreSQL with lease-expiry redelivery (test_phase28_build_interruption.py, 2 tests); retained journeys 12 passed |
| 28.5 agents/workflows | VERIFIED LOCALLY | required-child failure blocks success; optional child settles NAMED under best_effort (missing now names failed children on SQLite + Firestore); test_phase28_child_dispositions.py, 3 tests |
| 28.6 recovery/learning | VERIFIED LOCALLY | promotion gate compares against the DETERMINISTIC floor, not seeded-random (test_phase28_recovery_learning.py, 3 tests); RL real-task suite exit 0 |
| 28.7 usage/frontend | VERIFIED LOCALLY | metered receipts name the RESOLVED provider; sync lifecycle callable from async contexts (Phase 26 refactor repaired); stale TokenStatusCard copy fixed; 25 backend + 91 frontend green |
| 28.8 data/deliverables | VERIFIED LOCALLY | exported-file verifier: facts must survive HTML→PDF conversion; mislabeled/corrupt export fails (test_phase28_exported_deliverable.py, 3 tests) |
| 28.9 3D/modeling | VERIFIED LOCALLY | units+material in the spec; dimension edit measurable in downloaded OBJ bbox; cancelled export publishes nothing (test_phase28_modeling.py, 3 tests); J24 retained 14/14 |
| 28.10 providers/deployment | BLOCKED (B1 + cloud targets — release owner) | local engineering done: tag-only manifest rejection pinned; hosted provider receipts need credentials |
| 28.11 restore/measurement | VERIFIED LOCALLY | app-restore drill 14/14 checks (exit 0); workload measurement 6/6, 100% verified, 0 unauthorized, 0 duplicate effects, p50 10.2s / p95 14.2s; negative regressions pinned |
| 28.12 pilot/release | EXECUTED per attempt; BLOCKED (B4) for the genuine pilot | 4 qualifier attempts retained; journey evidence attached to attempt bad92192… (16/24 journeys PASS, failures trace to covering stages); final verdict + per-target table in the register |

## Phase 30 — harden authority, recovery, learning and usage (14 Nov)

Consolidated register: `docs/qualification/zcode/phase-30.md`. Every row
keeps its original ID and its Phase 30 owner; a row marked VERIFIED
LOCAL must name its receipt, and rows with external prerequisites stay
BLOCKED until their actual evidence exists (cloud/pilot gates stay open).
Historical passes remain attributed to their own candidates
(Phase 25 `…94889c928cb9`, 26 `…da556e6ab253`/`…044391f3768b`,
27 `…750ef76f1eba`, 28 baseline `…7bb05735d98f`/`…a4929d196391`,
29 `…3b5f9b9cb68c`) — never summed with current-tree runs.

Owner: Zcode (this chat). Phase 26 implementation owner: the originating
Codex chat (26.R1–R12, 26.P1–P4 remain the upstream IDs; Phase 30
consumes their landed fixes under the original IDs).

### Master register (30.0)

| Phase 30 ID | Original ID(s) | User entry point | Status | Receipt / blocker |
|---|---|---|---|---|
| 30.0 master register | ledger rules | this table + `test_phase30_ledger.py` | IMPLEMENTED | ledger check test enforces owner+status+receipt/blocker per row; orphans/duplicate owners fail |
| 30.1 supported profiles | 22/25 scope | release-scope + supported-profiles docs | CARRIED (28/29 verified) | `docs/release-scope.md`, `docs/qualification/supported-profiles.md` |
| 30.2 shared contracts | 26.R2/27.2/28.2 | engine/worker/runner/browser records | VERIFIED LOCAL | three-store contract suites (29.1 receipts) |
| 30.3 evidence integrity | 27.0–1, 28.1 | qualifier + journey producer | VERIFIED LOCAL | `test_phase28_evidence_gate.py`, `test_phase29_qualifier_gate.py` (journey postconditions: run ID + observed result required; `Qualifier.attempt_dir` regressions fixed) |
| 30.4 candidate provenance | 24/28.1 | freeze/verify candidate scripts | VERIFIED LOCAL | manifest self-consistency (29.3); reconstruction receipts |
| 30.5 acceptance matrix | 28 register | acceptance-matrix doc | EXTENDED | docs/qualification/acceptance-matrix.md phase-30 rows + test_phase30_ledger.py |
| 30.6 operator authority | 1.2/26 worker gate | auth + permission matrix | IMPLEMENTED | named OPERATOR_ACTIONS (decrypt/key-admin/policy-approve separated); redacted receipts; receipt: test_phase30_envelope_and_authority.py |
| 30.7 credential/envelope | 27.4/29.5 | credentials API + vault | IMPLEMENTED | env1 envelope w/ tenant/purpose AAD + rewrap (`backend/security/key_envelope.py`); GCP version-qualified refs; dual reader; bounded CAS backfill; receipt: test_phase30_envelope_and_authority.py |
| 30.8 reserve/settle | 2/29.6 | budget service | IMPLEMENTED | conflicting-receipt reconciliation via reconcile_conflicting_receipt (original preserved, idempotent correction); receipts: test_phase30_envelope_and_authority.py, test_phase30_usage_ledger.py |
| 30.9 effect safety | 5/29.7 | effect/reconciliation services | VERIFIED LOCAL | lost-write → unknown → reconcile; connector writes in ledger (29.7 receipts) |
| 30.10 fair admission | 3 | admission/capacity services | VERIFIED LOCAL | retained admission/capacity drills |
| 30.11 durable recovery | 4/26 | worker/outbox/reconciler | VERIFIED LOCAL | kill-before/after-persist drills (26/28 receipts) |
| 30.12 runner ownership | 26.R1 | runner journal | VERIFIED LOCAL | migration 0021 journal + reconcile_runner_once (26 receipts) |
| 30.13 egress broker | 27.3 | egress_broker | VERIFIED LOCAL | allow/deny matrix + redirect revalidation (27 receipts) |
| 30.14 browser parity | 26 | browser_session_repository | VERIFIED LOCAL | typed contract + CAS on 3 stores (26 receipts) |
| 30.15 viewer page | 26.R3 | browser_viewer | VERIFIED LOCAL | two-process real-Chromium journey (25/28 receipts) |
| 30.16 DOM dispatch | 27.D2/D3 | WebAgent._validate_dispatch | VERIFIED LOCAL | generations + VALIDATION_UNAVAILABLE (27/28 receipts) |
| 30.17 human takeover | 26/28.3 | BrowserSessionPanel | VERIFIED LOCAL | controller CAS + takeover drills (28 receipts) |
| 30.18 app intake | 29.14 | integrations router | VERIFIED LOCAL | intake record persisted pre-generation (29 receipts) |
| 30.19 worker builds | 26.R2/29.15 | MCP factory | VERIFIED LOCAL | standalone build + interruption drills (26/28 receipts) |
| 30.20 exact-byte validation | 29.16 | protocol validator | VERIFIED LOCAL | suite-digest-bound receipts (26 receipts) |
| 30.21 activation/resume | 26/29.17 | connector_activation | VERIFIED LOCAL | CAS activation + parent wake (26 receipts) |
| 30.22 catalog/install | 27.6/29.18 | catalog page | VERIFIED LOCAL | J23 marketplace journey |
| 30.23 repair/migrate | 29 | connector_repair_service | VERIFIED LOCAL | drift → sibling revision staged, never merged (29 receipts) |
| 30.24 durable triggers | 29.19 | templates/schedules/webhooks | VERIFIED LOCAL | plan-version hash + tz-correct schedules (29 receipts) |
| 30.25 verified ReAct | 29.20 | outcome_verifier | VERIFIED LOCAL | bounded-stop + verifier writeback (29 receipts) |
| 30.26 enabled-agent inventory | 26.R4/29.21 | agent factory | VERIFIED LOCAL | 13-role inventory joined to real receipts (27/29 receipts) |
| 30.27 coordination | 29.22 | agent_contracts | VERIFIED LOCAL | contradiction → `disputed` named (29 receipts) |
| 30.28 bounded self-healing | 11/29.23 | failure_classifier + recovery_worker | IMPLEMENTED | ack_lost/timeout_before_dispatch/stale_page classes; episode VERIFIED_RECOVERY only on original postcondition; receipts: 29.23 suites + test_phase30_envelope_and_authority.py |
| 30.29 evaluations | 29.24 | evaluation_datasets | VERIFIED LOCAL | split audit + refusal scoring (29 receipts) |
| 30.30 algorithm choice | 14/27.D9/29.25 | contextual_policy + trainer | VERIFIED LOCAL | deterministic baseline + promotion_floor (29 receipts); eligible-only ranking; offline replay propensities |
| 30.31 registry/canary/rollback | 26.R5/29.26 | improvement service | EXTENDED | predecessor/lineage + stop-reason registry fields; subgroup-support hold wired into promotion; receipt: test_phase30_learning_governance.py |
| 30.32 usage truth | 27.D4/D5/29.27 | usage router + UI | IMPLEMENTED | normalizer cached-input + nested details; provider rate-limit fields; pricing service (versioned tariff); metered model resolved_model/request-id; receipts: test_phase30_usage_ledger.py, test_phase30_usage_normalizer.py |
| 30.33 memory lifecycle | 16/29.28 | memory_lifecycle | VERIFIED LOCAL | durable cleanup jobs + dependency edges + restore drills (26 receipts) |
| 30.34 feedback→fix | 26.R7/29.29 | feedback journey | VERIFIED LOCAL | controlled journey + retention sweep (26 receipts) |
| 30.35 deliverables | 17/29.30 | rendered_document_verifier | VERIFIED LOCAL | export verifier + digest-versioned previews (28 receipts) |
| 30.36 reconnect/stale | 27.D6/29.31 | useRunEvents + run-subscriptions | VERIFIED LOCAL | online reconciliation + auth generation (27/29 receipts) |
| 30.37 workspace UI | 27.14/29.32 | workspace page | EXTENDED | unmet-criteria list + usage panel + actor/policy line; receipts: frontend mounted tests + backend usage API tests |
| 30.38 preview isolation | 28.7a | link_previews router | EXTENDED | principal+epoch partitioned preview cache; private per-access grants; receipts: 28.7a suites + test_phase30_preview_partition.py |
| 30.39 3D optional | 27.D10 | CoordinationGraph | CARRIED | 2D parity retained; J24 |
| 30.40 model assets | 27.D10/29.33 | modeling service | EXTENDED | versioned edits via public path (edit → child job → redownload prior revision); receipt: test_phase30_modeling_versions.py |
| 30.41 hosted inference | 26.R8/28.10 | provider adapters | BLOCKED (B1) | no authorized staging credentials — release owner; adapters + readiness ladder exist |
| 30.42 immutable topology | 26.R10/29.35 | helm values | BLOCKED (B7) for cloud | local digest readback verified (29 receipts) |
| 30.43 restore | 26.R9/29.36 | qualify_restore_application | EXTENDED | envelope-generation restore checks (both generations decrypt after restore); receipt: qualify_restore_application PASSED 16/16 (2026-11-14, recovery 11.086 s local) + test_phase30_envelope_and_authority.py |
| 30.44 workload measurement | 26.R11/29.37 | workload_measurement | VERIFIED LOCAL | planned/terminal/verified per family + denominator gates (28 receipts) |
| 30.45 defensive review | 27/28 | security suites | EXECUTED | security stage retained; findings recorded in docs/qualification/security-findings.md |
| 30.46 pilot/drills | 26.R12/29.38 | pilot plan | BLOCKED (B4) | no consented participants — release owner |
| 30.47 release decision | 26.P4/29.39 | qualifier --strict | EXECUTED (see report) | candidate 20261114-162557-0fbae3b0bc47 RECONSTRUCTIBLE; strict attempt in docs/evidence/phase-30/execution-report.md; canary/rollback readback remains with the release owner |

Check rule (enforced by `backend/tests/test_phase30_ledger.py`): every
row above carries exactly one primary owner, a status, and either a
receipt or a named blocker; a row may not claim VERIFIED/IMPLEMENTED
without a receipt reference; original IDs are never dropped.
