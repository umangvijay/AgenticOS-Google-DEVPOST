# Changelog

## v44.0.0 — 2026-11-14

### Phase 30: harden authority, recovery, learning and usage — local engineering exit

- **Envelope encryption (30.7)**: `backend/security/key_envelope.py` — env1
  records authenticate tenant/credential/purpose/format as AEAD associated
  data; version-qualified managed references (`gcp://name@version`); dual
  reader across env1/managed/legacy generations; bounded CAS backfill with
  checkpoint + conflict/corrupt classification. Credentials API writes
  envelopes; restore drill proves BOTH generations decrypt and wrong-tenant
  opens are refused.
- **Operator authority (30.6)**: named `OPERATOR_ACTIONS` with decrypt,
  key-administration and policy-approval held as SEPARATE permissions;
  redacted support receipts.
- **Settlement (30.8/30.32)**: conflicting receipts preserve the original and
  book the delta through an idempotent audited correction; the worked ledger
  (reserve → settle → dedup → uncertain → late receipt) is a pinned test;
  versioned tariff pricing (`tariff-2026-11`), resolved model + provider
  request ids on receipts, provider rate-limit surface (explicit unavailability,
  never a guessed reset).
- **Classifier (30.28)**: `ack_lost` vs `timeout_before_dispatch` vs
  `stale_page` dispositions — a lost acknowledgement is never retried blind.
- **Learning governance (30.30/30.31)**: offline-evaluation HOLD (subgroup
  support, deterministic-log overlap) blocks promotion; candidate registry
  lineage (predecessor, split, mask/reward versions, stop reason) rides the
  pinned manifest.
- **Usage/UI (30.32/30.37)**: OpenAI cached-input + nested-details
  normalization; `EvaluationPanel` names WHICH criterion remains unmet;
  workspace mounts the budget panel; provider rate-limit line in
  `BudgetUsagePanel`.
- **Previews (30.38)**: cache partitioned per principal (+ epoch keys);
  private descriptors stay never-cached; cross-account refusals retained.
- **Modeling (30.40)**: versioned edits via the public path — edit creates a
  child job (v+1), prior revisions stay downloadable, cancelled edits publish
  nothing.
- **Register**: Phase 30 master register in `docs/REQUIREMENT_LEDGER.md` with
  an executable integrity check; acceptance-matrix rows; execution report at
  `docs/evidence/phase-30/execution-report.md`; security findings recorded.
- Cloud/pilot/hosted gates remain OPEN (B1/B4/B7/B8 — release owner).

## v43.0.0 — 2026-11-08

### Phase 22: full-system qualification — locally verified release candidate

- **Strict qualification harness** (`scripts/qualify_release.py`): 17 mandatory
  stages, per-test deadlines, readiness gates against the PRODUCTION stack,
  named-retry recording (never merged into first attempts), declared-blocker
  accounting (B1 credentials / B4 pilot), strict mode.
- **Recorded results** (docs/evidence/phase-22.md + phase-22-qualification.json):
  unit_contract 1494/1494 (final), infra_integration 60/60,
  live_inference_local 14/14 (REAL Ollama), fault_injection 35/35,
  security 60/60, product_e2e vitest/typecheck/lint/build PASS +
  browser 28/28 serial, flagship + restore + target_deployment stages added
  and executed; live-stack journeys 13/13 (live_e2e) + 18/18 (live probes).
- **Integration gaps closed before qualification** (audit 22.0 findings):
  Bedrock/Ollama provider acceptance + ordinary dispatch, Postgres factory
  with full inventory, browser-viewer router mounted, real RL environment,
  feedback router — each recorded with executable proof in the Phase 21
  packet (the audit's "still present" list was stale; re-verified).
- **Defects found by real execution and fixed**: S1 revocation gap,
  S2/S3 dependency CVEs, S4 invitation 500, S5 ownership invisibility,
  S6 planner bypass, S7 budget schema divergence + 9 more
  (docs/qualification/security-findings.md).
- **Isolation executed** (not assumed): 12/12 hostile fixtures against the
  sibling-container runner (the docker-SDK context skip bug fixed).
- **Backup/restore + upgrade/rollback + k8s**: hot pg_dump → separate
  restore (counts match, 0 orphans), real upgrade cycles with persisted
  data + rollback drill, kind cluster helm deploy with journey COMPLETED
  (four chart defects fixed live).
- **J01–J22**: all 22 mandatory journeys PASS (evidence index in
  docs/evidence/phase-22.md) except the credential-gated hosted-inference
  leg of J21 (B1) and the human-pilot execution (B4 — plan ready,
  execution pending real participants).

### Readiness
Locally verified release candidate on the durable profile. Remaining
blockers: B1 (hosted credentials — operator action), B4 (real pilot
participants), B2 (NATS-in-colima networking; Postgres outbox is the
proven durable path). Phase 23 handoff: docs/phase-23-handoff.md.

## v42.0.0 — 2026-11-07

### Phase 21: the autonomous, self-extending workspace — integrated and evidenced

- **Durable platform runs for real**: compose `core` boots API + worker as SEPARATE processes over Postgres 16; a submitted goal is planned + executed by the worker (transactional outbox dispatcher + direct task runner) and COMPLETES with real output; a full stack restart RESUMES interrupted runs to COMPLETED (test_phase21_stack_journeys).
- **PostgreSQL is a first-class backend**: the REAL factory initializes the full inventory (users, sessions, orgs, credentials, tools, §4 commands+outbox, effects, browser ownership, recovery, evaluations, memory, deliverables, feedback, budgets); migrations 0001–0013 with the checksum ledger (it caught two accidental edits — working as designed); §2 budget state machine + §4 command contract verified live.
- **NATS JetStream**: pull-based shared consumers; loud failure when unreachable; container fetch hang recorded as a limitation (outbox dispatcher is the container delivery path).
- **Providers through ordinary execution**: OllamaADK + BedrockADK; REAL local inference (qwen2.5:3b) with prompt-dependent output, tool calling, and budget-metered usage; per-user credential binding + the four-state readiness ladder verified end-to-end locally.
- **Proven isolated runner**: sibling-container backend enforcing ALL TEN isolation acceptance fixtures (host secrets unreadable, egress denied, OOM killed, fork bounded, cancel SIGKILLs, descendants die, records durable).
- **Flagship journey AUTOMATIC**: parent WAITING → durable build → child completes → §4.13 reconciler wakes the parent — no manual flips.
- **Live browser takeover**: stale epochs refused, server-enforced pause, epoch rotation audited, return-control forces fresh observation, REAL PNG frames (sha256-verified) in the wired panel.
- **Genuine RL**: real state-changing recovery tasks (files regenerate/rewrite/read back; service downtime decrements; two-phase source requests; escalation for poisoned states); parameters change; checkpoints reload; held-out eval reproducible; promotion governance-shaped.
- **Injected healing**: interruption/429/reset/422/uncertain-outcome — real injections replacing the vacuous test.
- **Durable memory dependencies**: edges survive restarts; correction propagates; failed cleanup retried visibly (repo now re-queues failed jobs).
- **Real-user feedback**: full journey (submit → triage → deletion → retention) with explicit diagnostic consent; never a training reward.
- **Packaging corrected**: worker entrypoint, port alignment, requirements completeness, helm commands, bootstrap script; lint --max-warnings=0 clean.

Final regression: **1491 passed, 0 failed** (backend, excluding the resource-isolated RL suite: 3 passed separately). Frontend: vitest 88/88, tsc clean, lint clean, build passes, e2e 5 passed. Evidence: docs/evidence/phase-21.md + phase-21-carryovers.md.

### Blockers closed during final pass

1. **NATS container delivery**: the pull-consumer consumes the backlog
   live in-container + a fresh journey COMPLETED through NATS delivery.
2. **Interactive takeover journey**: the full 8-step deployed-stack
   journey (open → frame → pause → takeover → user acts → return →
   re-observe → stop) passed and is locked as a test.
3. **Local Kubernetes**: kind cluster + helm deploy (image-helper bug
   fixed live) + full journey COMPLETED on kind.
4. REMAINING (credential-gated): Vertex/Bedrock LIVE inference — no
   credentials exist on this host; every other aspect proven locally.

## v41.0.0 — 2026-11-06

### Phase 20: local-first completion — every mandatory gate with passing evidence

All §20.0–§20.29 gates executed for real on this machine; the honest
matrix with per-gate evidence is [docs/evidence/phase-20.md](docs/evidence/phase-20.md).

- **Live infrastructure gates**: PostgreSQL 16 contract suite (8 tests —
  migrations + checksum ledger, atomic lease claim, idempotent commands,
  ordered event replay, revisioned deliverables, budget reserve/settle,
  tenant isolation) and NATS JetStream suite (11 tests — live
  publish→consume→ack, `Nats-Msg-Id` dedup, bounded redelivery, DLQ,
  ordered replay) run against REAL servers (colima started; compose
  `core` gains postgres on :55432).
- **Firestore emulator acceptance (§20.13)**: fail-closed anti-production
  guard (unset env / non-loopback / real credentials each handled
  explicitly), live smoke contract, and the full 48-test parity set with
  ZERO skips. Found and fixed two real defects: a stale `delegate`
  contract in the parity suite, and lockstep transaction retries
  exhausting the emulator's lock window → jittered `txn_retry.py`
  hardening that also helps production contention.
- **Artifact store (§20.6)**: immutable revision identities, per-read
  digest verification, existence never leaked to non-owners, grant/revoke
  downloads, chain-safe delete (11 tests).
- **Browser execution + viewer/takeover (§20.15–§20.16)**: real
  Playwright service vs a controlled local app (7 tests), server-enforced
  ownership router (6 tests — strangers 403, takeover rotates the
  controller epoch audited, viewer-role refused by the REAL guard), and
  the mounted BrowserSessionPanel (4 tests).
- **Real-backend browser journeys (§20.12)**: `e2e/local-backend.spec.ts`
  — guest session → real composer submission → real engine execution →
  COMPLETED in the workspace → API cross-check; loud reachability failure
  with the runnable command when the backend is down.
- **Durable ReAct restart recovery (§20.18)**: new engine over the same
  db resumes stranded tasks, committed effects are never re-dispatched
  (dispatch counted), deferral timers survive restart.
- **Healing/evolution/RL**: every failure class injected (15 tests);
  promote/reject/rollback over the real ImprovementService (8 tests);
  real PPO training with checkpoint + held-out eval (3 tests).
- **Telemetry correlation (§20.25)**: journey ids on every span and log
  line, inherited across stages, secrets redacted; wired into the
  engine's task spans.
- **Portable packaging (§20.27)**: helm chart (lints + renders; hardened
  runner securityContext) + docs/cloud-portability.md.
- **Docs (§20.28)**: local-production, provider-authentication,
  local-runner, browser-viewer, learning-pipeline + README updates.
- **Frontend gates**: vitest 88/88 (mounted panel added), tsc clean,
  `lint --max-warnings=0` clean (10 pre-existing
  `react-hooks/set-state-in-effect` errors fixed with render-time
  adjustment / derived state / ref-in-effect patterns), production build
  passes.
- **Full backend suite: 1428 passed, 0 failed, 0 skipped** (includes the
  live emulator/NATS/Postgres suites). Phase 20 gate suites: 115 passed
  guarded; loud skips only when the emulator env is unset.

### Honest remaining blocker

- §20.8 LIVE Vertex/Bedrock inference needs real credentials
  (`GOOGLE_API_KEY` / `AWS_BEARER_TOKEN_BEDROCK`); the request-shaping,
  credential-validation, and error-normalization contracts all pass
  locally. Every other gate ran for real on this machine.

## v40.1.0 — 2026-11-06

### Phase 18 completion: production wiring for the remaining carryovers

- **Subscription ownership live**: the workspace page and useRunEvents
  both consume the shared reference-counted manager — no overlapping
  owners (F4 closed at the call sites).
- **Evidence panel mounted**: dashboard-wide ConnectionStatus, the
  workflow page's deep-linkable evidence view (?deliverable=...), and
  the resume page (from v39.1).
- **Correction propagates to deliverable staleness**: correcting a
  source memory marks dependent verifications STALE through the durable
  dependency graph (the accept test runs through the REAL service with
  the factory-registered accessor). Propagation failures log WARNING.
- **Verifier hardening**: coroutine objects and unrecognized types are
  REFUSED — truthiness never promotes them (§18.3 probe).
- **Three suggested components landed**: RunStatusBanner, ConnectionStatus
  (dashboard-wide offline notice), MutationConflictNotice.
- Full suite: **1230 passed, 0 failed, 48 skipped**; frontend vitest
  72/72, tsc clean.

## v40.0.0 — 2026-11-06

### Phase 18: React reliability, session resilience, and Phase 17 carryover closure

All 33 subphases (18.0–18.32); findings C1–C14 and the ten frontend
defects registered at §18.0 with production entry points:

- **C7/C8 (publication durability)**: publication progress moved into a
  DURABLE publication_jobs table (owner, lease, stage, attempts, error)
  — a production worker claims expired leases, re-drives the staged
  sequence from the recorded stage, and parks exhausted jobs as FAILED
  (visible). `require_production_deps` makes storage/verification/
  emission mandatory for production publications.
- **C9/C10 (reviews change the deliverable)**: remove/edit MUTATE the
  document content (claim stripped/replaced in the HTML), create a NEW
  re-verified revision, and run the atomic expected-revision check
  against the key's latest revision — the loser gets a 409 with the
  current revision and next action.
- **C12 (cleanup wired)**: memory_cleanup_worker + deliverable_worker
  run on the production tick; the memory service's forget() propagates
  deletions to deliverable invalidation; the abstention mapping now
  agrees with the constructed keys.
- **C13 (per-invocation accounting)**: a metered-model wrapper gives
  every actual model invocation its own identity and reservation inside
  the outer runner bound.
- **18.8 (state contracts)**: run-state.ts separates execution/outcome/
  connection/freshness/mutation/artifact — connection failure degrades
  the VIEW, never the task; terminal states never revert.
- **18.9/18.10 (request layer)**: request-policy.ts COMBINES caller and
  timeout signals (an already-aborted request never reaches the
  transport — the reviewer's probe 4) with one total deadline across
  CSRF/refresh/retry; the composer keeps the SAME idempotency key on
  ambiguous timeouts and explains the acceptance state.
- **18.11/18.12/18.13 (auth)**: the auth state machine + generation
  identifier (stale generations discarded); the no-Web-Locks refresh
  path uses a cross-tab lock — non-owners RE-READ the rotated session
  instead of racing the single-use token; the session architecture
  decision documented (token client retained).
- **18.14–18.18 (subscriptions/polling)**: reference-counted
  run-subscriptions (one owner per run, coordinated fallback poller);
  the replay cursor is an OPAQUE STRING (the integer conversion that
  corrupted UUID cursors removed); the SSE dedup window is BOUNDED;
  useNetworkStatus tracks online/offline separately from quality;
  useCommandStatus decides from the CURRENT result.
- **18.19–18.22 (drafts/review)**: the versioned identity-scoped draft
  store (restoration completes before empty values overwrite; ChatComposer
  ADOPTS the stored draft — finding F1); the evidence panel carries
  generation guards, review error handling, stale-verification display,
  and last-refresh time.
- Full suite: **1227 passed, 0 failed, 48 skipped**; frontend vitest
  64/64, tsc clean. New docs: frontend-state-contracts.md,
  event-reconciliation.md, session-and-refresh.md,
  runbooks/frontend-recovery.md.

## v39.1.0 — 2026-11-05

### Phase 17 review-gap closure: production integration + three new probes

The second review found the deliverable repository test-only, the
publisher/verifier without production callers, the evidence panel
unmounted, and three new false-positive probes. All closed:

- **Factory initialization**: `deliverable_repo` is initialized on BOTH
  stores (SQLite `DeliverableRepository`; new
  `FirestoreDeliverableRepository` with transactional revision
  allocation) and registered through the module accessor.
- **Production callers**: `save_user_files_with_delivery` wraps the
  builder's save path — the orchestrator's create_files tool now records
  the durable deliverable, publishes through the staged sequence, and
  verifies with the typed verifier; the resume generation route
  (`/resume/generate`) does the same and returns the `delivery` payload.
- **Panel mounted**: ArtifactEvidencePanel renders on the resume page
  for the recorded deliverable (absent when none exists — never an
  empty "all checks passed"); the verdict badge mapping moved to the
  shared lib (`artifactVerdictStyle`) — the panel and the vitest import
  it, no copied logic.
- **New probe 1**: a typed failure result as an OBJECT (dataclass/model)
  now FAILS — the criterion reads `verdict`/`verified` from dicts and
  objects alike; bool(obj) is never evidence.
- **New probe 2**: a checksum string WITHOUT a byte check is
  `needs_review` — the verifier refuses to pass on metadata alone.
- **New probe 3**: required text present only in an HTML COMMENT now
  FAILS — `text_survives` checks the EXTRACTED visible text (comments/
  script/style excluded), not the raw HTML.
- Full suite: **1211 passed, 0 failed, 48 skipped**; frontend vitest
  56/56, tsc clean.

## v39.0.0 — 2026-11-05

### Phase 17: verified deliverables, provenance, and carryover closure

All 28 subphases (17.0–17.27) and the review's confirmed findings
A–H, closed through PRODUCTION paths:

- **A**: the orchestrator's memory tools now run through the ONE
  application-level memory service (`memory_service.py`) — the write
  policy strips server-owned fields the model tried to set; retrieval
  (semantic AND keyword fallback) applies the same visibility rules and
  keeps provenance/uncertainty labels in the agent context.
- **B**: memory correction honors version progression 1→2→3; supersession
  is a repo contract on both stores; forgetting a correction NEVER
  reactivates the older fact; deletion creates durable, claimable
  cleanup jobs (retryable across restarts).
- **C**: the Firestore memory store gained the full lifecycle parity —
  transactional create-or-get with the identity at the queried top-level
  location, supersession, dependency edges, cleanup jobs; the factory
  wires the lifecycle for both stores; the source-event identity is
  (user_id, event_id), not global.
- **D**: the ENGINE's runner execution now holds its own metered-call
  reservation (per model attempt; usage events ACCUMULATE across
  multi-turn invocations instead of replacing); memory correction embeds
  through the metered path and records "unavailable" instead of a
  fabricated vector; budget denial prevents the call.
- **E**: all four reproduced false positives CLOSED — a changed employer
  under the same key is a CONTRADICTION; a self-declared source_fact
  without a verified source is UNBACKED; 10%→90% and negation flips are
  detected; the negative artifact result dict FAILS (never bool()).
- **F**: tailoring recomputes provenance/groundedness AFTER the edit
  (fail-closed — no exception-to-grounded shortcut); the loose
  word-overlap check is replaced by the fact checker's contradiction
  logic.
- **G**: the durable deliverable contract (models/repo/Firestore-parity
  planned) records identity/content/lineage/verification/lifecycle;
  staged publication survives injected crashes at every stage; manifest
  integrity requires sha256+length per file, rejects escaping/reserved
  paths, and binds verdicts to actual bytes.
- **H**: the overstated matrix rows now use AUTHORIZED nonempty fixtures
  (the forgotten-memory scenario proves the deletion through semantic
  AND keyword paths for the SAME user).
- **17.14–17.17**: rendered-document verification (parses, text
  survives, sections ordered, safe links, no remote-fetch vectors);
  the resume preview sandbox (`sandbox=""`) and Studio preview isolated
  (scripts without same-origin); the ATS heuristic names its rubric
  version/weights and what it is NOT; generated projects carry honest
  execution states (unexecuted = no badges, runner stays unavailable).
- **17.18–17.20**: bounded deliverable self-healing (typed repairs;
  missing dates → clarification, never invention; exhaustion →
  escalation); five deliverable specialists registered within
  boundaries; document-case evaluation families and byte-bound reward
  eligibility with versioned verifier corrections.
- **17.21–17.23**: the ArtifactEvidencePanel (checks, claims, files,
  review actions bound to revisions; a fetch error is never "all checks
  passed"); the /deliverables API with 409 revision-mismatch review.
- **17.24–17.27**: source corrections mark dependent verifications
  STALE (history preserved); the complete 16-step journey passes;
  docs/deliverable-verification.md, docs/document-provenance.md, and
  docs/runbooks/deliverable-failure.md added.
- Full suite: **1199 passed, 0 failed, 48 skipped**; frontend vitest
  56/56, tsc clean.

## v38.0.0 — 2026-11-05

### Phase 16 findings A–K: the governed memory wired to the HTTP boundary

The completeness audit found ALL THIRTEEN Phase 16 services were
test-only — the memory API bypassed every governance gate. Closed:

- **A (the headline)**: the memory router now runs the GOVERNED paths —
  `add` validates the candidate and applies the write policy (secrets
  refused 422, inferred facts stored as needs_confirmation with the
  decision in the response); `search` derives scope server-side, runs
  governed retrieval, and reports honest abstention; `list` carries the
  governed fields on every path.
- **B**: hostile stored content is FLAGGED on retrieval results
  (`hostile_flag`) — flagged or not, every row is untrusted data.
- **C**: forget runs the deletion WORKFLOW (immediate hidden barrier →
  retryable `/cleanup` endpoint) — an immediate delete no longer
  pretends cleanup happened.
- **D**: corrections register their dependency edge at the API layer
  (the propagation map is real, not a test fixture).
- **E**: the worker records execution EPISODES from evaluated runs —
  the honest outcome class travels with the episode (release-scoped).
- **F**: verified episodes propose procedure CANDIDATES (needs
  confirmation; never auto-activated; gated on the reviewed-procedures
  stage).
- **G**: delegated contracts carry the specialist's NON-WIDENING memory
  scope in their input — the child cannot issue an unrestricted search.
- **H**: the export withholds ENTIRELY when secret-shaped content is
  detected in the payload (defense in depth behind the write gate).
- **I**: the dependency map exists at the API layer and registers
  corrections; dependents invalidate with their source.
- **J**: `MEMORY_STAGES_ENABLED` joins settings — bounded auto-extraction
  and broader personalization are opt-in.
- **K**: `/memory/effect` (admin) exposes the with/without-memory
  evaluation — unmeasured windows abstain honestly.
- Full suite: **1130 passed, 0 failed, 48 skipped**; frontend vitest
  52/52, tsc clean.

## v37.0.0 — 2026-11-05

### Phase 16 complete: governed memory and retrieval

All 32 subphases (16.0–16.32) implemented on the EXISTING lifecycle
(no competing memory system):

- **16.0–16.2 inventory/scopes/record**: every memory path has a named
  storage, retrieval, and lifecycle operation; the record gains the
  EXPLICIT sharing scope (task/workflow/personal/project/organization/
  published), verification status, sensitivity, observed time,
  extraction metadata, source-event identity, and deletion state — a
  retrieved statement traces to its source, owner, and current status.
- **16.1 (the headline guarantee)**: org membership NEVER implies
  visibility — organization-scope retrieval requires an explicitly
  shared reference; an inference can never publish; adding a member
  exposes nothing historical.
- **16.3/16.6 write policy**: extraction PROPOSES, the server decides —
  explicit preferences store confirmed (no re-asking); inferred facts
  store as inferred needing confirmation; procedures need review; the
  model cannot self-confirm or widen visibility; malformed candidates
  are refused.
- **16.4 secrets**: secret-shaped content (keys, tokens, JWTs, private
  keys) is refused at the policy gate — only credential-store
  references belong in memory.
- **16.5 durable writes**: create-or-get on the source-event identity —
  event replay cannot multiply memories (SQLite partial unique index +
  Firestore query parity); a crash between storage and indexing loses
  nothing.
- **16.7–16.9 dedup/supersession/freshness**: same-content records for
  different users stay separate; context differences coexist while
  explicit corrections supersede with history; expired assumptions stop
  being served.
- **16.10–16.13 retrieval**: ONE authorized path — ownership and
  lifecycle filters run BEFORE scoring; org rows enter only through
  shared refs; exact/keyword(word-boundary)/semantic methods combine
  with the signals SEPARATE (relevance × verification × freshness);
  context is bounded with statuses traveling verbatim.
- **16.14 content-as-data**: every retrieved row is untrusted
  tool/page content — hostile stored instructions are flagged and can
  never carry authority.
- **16.15 specialists**: per-role memory filters bind the retrieval
  path and ride through handoffs — children cannot widen access.
- **16.16–16.18 episodes/procedures**: episodes record honest outcome
  classes; procedures carry preconditions and verification criteria,
  cannot self-promote, and revalidate CURRENT gates before reuse (a
  remembered procedure cannot bypass today's checks).
- **16.19–16.20 preferences/facts**: task instructions override
  preferences visibly; generated drafts stay inferred — never
  authoritative user facts.
- **16.21–16.22 correction/deletion**: a dependency map invalidates
  registered dependents (stale summaries cannot resurface); deletion is
  marker → immediate retrieval barrier → observable retryable cleanup
  with stated retention boundaries.
- **16.24 migrations**: versioned index rebuilds — coverage-tracked,
  validated, deliberately switched, rollback-safe; dimension changes
  force rebuilds (never silent vector mixing). Fixed: the governed
  indexes moved into the migration (in SCHEMA_SQL they broke
  pre-existing databases).
- **16.25–16.26 quality/effect**: the abstention contract returns "no
  reliable memory found" rather than manufacturing answers; the
  with/without-memory effect evaluation keeps missing measurements
  missing.
- **16.27–16.29 workspace**: the memory page (source/scope/status
  directly visible; correct/forget/export), server-derived lifecycle
  states (vitest-proven: "deleted" is never a vanished card), and the
  relationship view over REAL supersession edges.
- **16.30–16.32 parity/journey/release**: the Firestore memory repo
  gains get_memory + governed fields + create-or-get (surface parity);
  the 12-step flagship journey passes through the real services; the
  memory stage flags keep collection/retrieval/automatic-reuse
  independently configurable.
- Full suite: **1120 passed, 0 failed, 48 skipped**; frontend vitest
  52/52, tsc clean.

## v36.0.0 — 2026-11-04

### Phases 14–15 closure audit: every service has a REAL production caller

The second completeness pass found six specialist services with ZERO
production callers and two Phase 14 integration bugs. All closed:

- **W1 (15.30)**: the release-stage gate is now ENFORCED INSIDE
  `delegate()` — a role whose stage is off (or operator-disabled)
  refuses new delegation. The default stage set is HONEST: the four
  already-shipped stages (read-only, verification, connector
  generation, bounded recovery) are on; approved writes, browser
  workflows, and learned selection stay opt-in.
- **W2 (15.1)**: the engine records the DELEGATION DECISION (why this
  task delegated + which alternatives were considered) with the
  verification record.
- **W3 (15.17)**: the recovery worker now classifies the specialist
  failure through the response table BEFORE choosing a strategy — the
  permitted response travels on the episode.
- **W4 (15.21)**: resume building runs the fact-groundedness check —
  fields not traceable to the supplied profile are flagged as
  unsupported additions in the result.
- **W6 (15.12/15.20)**: the verifier returns a TYPED HANDOFF (results,
  evidence refs, limitations, unresolved questions — observations are
  never authority); the browser agent builds and validates a typed
  session contract at durable-session allocation and returns it in the
  result.
- **W7 (14.11 REAL BUG)**: the engine passed user-controlled
  `_task_category` straight into the context schema — an unknown
  category raised RoutingContextError and the routing decision was
  SILENTLY DROPPED. Fixed: the engine coerces unknown categories to
  the baseline bucket (the decision logs, bucketed; programmatic
  callers keep the strict schema). Also: `budget_units` now travel
  into the decision log — cost is actually MEASURED.
- **W8 (14.5)**: the durable browser checkpoint has a READER
  (`latest_checkpoint`) — recovery consumes the reference; the browser
  result carries the contract + checkpoint.
- Full suite: **1087 passed, 0 failed, 48 skipped**; frontend vitest
  50/50, tsc clean.

## v35.0.0 — 2026-11-04

### Phase 15 findings A–K: the honest audit of the specialist system itself

The Phase 15 machinery existed but several pieces were DECLARED without
being fully wired into production. The audit found the gaps; each is
now closed with its acceptance test:

- **A (dead recheck)**: the §15.5 authorization recheck never ran in
  production — no caller passed an authorization context. The runners
  now consult the configured provider by DEFAULT, and the app wires the
  provider to the real account store at startup: a deactivated user's
  queued specialist work stops with the typed AuthorizationLost before
  any contract is created.
- **B (unconsumed verdicts)**: the completion-policy verdict was repo-
  only; the engine consumes it at the delegation boundary (tested at
  the boundary in the coordination suite).
- **D (unattributed attempts)**: specialist attempts now flow through
  the SAME durable pipeline as model routing — the engine's delegation
  records a decision (mechanism 'delegation') whose verified outcome
  becomes the attempt's reward.
- **E (orphaned evaluator)**: `evaluate_specialization` gained its
  production surface (admin endpoint `/experiments/specialization/compare`).
- **F (orphaned learned selection)**: `delegate_specialist` records the
  release-scoped selection decision through the Phase 14 contextual
  policy when the 'routing' stage is enabled — logged probabilities in
  the decision log; OFF by default records nothing; authority unchanged.
- **G (stale panel)**: the workspace SpecialistPanel now reloads when
  run events advance (revision prop), so duplicate events cannot inflate
  anything and the view stays current.
- **H (unfenced Firestore)**: the Firestore contract transition lacked
  the §15.9 ownership fence — a stale owner could overwrite a
  replacement's result on the cloud store. Same fence as SQLite.
- **I (stuck RUNNING)**: a RUNNING contract past its deadline blocked
  its parent forever — `expire_stale_contracts` transitions it honestly
  to TIMED_OUT; the worker drives the sweep; Firestore parity added.
- **J (real bug: broken takeover)**: `claim` could NOT take over a
  RUNNING contract with an expired lease (worker loss was a silent
  stall, not a recovery path). Fixed: takeover reclaims RUNNING
  contracts with fresh leases; `reclaimable_contracts` surfaces the
  queue.
- **K (access control proven)**: the deliverable registry's ownership
  check (§12.28 — cross-tenant reads look identical to missing) is the
  §15.21 access control; proven with its test.
- **Context schema gap (found via D)**: the Phase 14 schema rejected
  'specialist_delegation'/'verification' categories — silently
  dropping the new decisions. Both are now legitimate buckets.
- Full suite: **1081 passed, 0 failed, 48 skipped**; frontend vitest
  50/50, tsc clean.

## v34.0.0 — 2026-11-04

### Phase 15 complete: bounded specialists and reliable coordination

All 30 subphases (15.0–15.30) implemented with named acceptance rows:

- **15.0 inventory / 15.1 delegation policy**: every advertised role's
  real execution path is queryable (placeholders explicit); the
  deterministic `should_delegate` decision records WHY and which
  alternatives were considered — simple tasks stay simple.
- **15.2 hierarchy / 15.3 complete contracts**: the delegation tree
  reconstructs from persistence (`subtree_of`); contracts now carry
  goal, exclusions, input refs, required capabilities, acceptance
  criteria, retry policy, and agent/tool/policy VERSIONS — a
  specialist cannot START with ambiguous authority or success
  (enforced at creation; the registry carries per-role default
  criteria).
- **15.4 authority / 15.5 recheck**: effective authority is the
  INTERSECTION of role whitelist × parent scope × user permission ×
  tool policy (prompt-declared roles grant nothing; approval-gated
  tools need a current grant); authorization is RECHECKED before claim
  in the production runner paths — access loss is a typed outcome
  (AuthorizationLost), never a transient retry.
- **15.6 bounds**: depth/fan-out caps, cycle detection, equivalent-work
  refusal (repeated delegation of the same goal is a cycle signal);
  CONCURRENCY now counts DISPATCHED work only (a real cap bug: queued
  intents made the fan-out cap unreachable).
- **15.7 budgets**: children RESERVE part of the parent's allowance —
  the cumulative RESERVATION bound (not settled spend) caps parallel
  children; each attempt keeps its own spend identity.
- **15.8 dispatch / 15.9 fencing**: crash-before-publication is
  recoverable (`pending_contracts` drain); terminal transitions are
  FENCED on the current claim (owner + live lease, atomic) — complete/
  fail require the actor to name its claim.
- **15.10 scheduling / 15.11 shared resources**: a stopped process is
  never a successful dependency (require_all/require_any verdicts name
  what is missing); one controller per browser session; artifact
  targets resolve through the dedup identity.
- **15.12 handoffs / 15.13 boundary validation / 15.14 verification**:
  typed handoffs with content-CLASS provenance (instructions embedded
  in tool/page content are untrusted data — they can never carry
  authority); output schemas enforced at the state transition; the
  verifier's criteria FREEZE onto the contract at delegation.
- **15.15 partial completion**: per-requirement states (incl.
  UNCERTAIN for unresolved effects) with reasons and next actions —
  partial external effects never auto-become verified achievement.
- **15.16 cancellation / 15.17 recovery**: cancelling a parent stops
  NEW child work (the cancelled-parent gate) and propagates to running
  children while preserving committed receipts; failures CLASSIFY
  before responding (uncertain writes → reconcile-first, authorization
  → stop, worker loss → reclaim, repeats → escalate).
- **15.18–15.21 paths**: the builder chain's gates (generated ≠
  available), the validator's FULL operational case set (empty
  responses, schema mismatches, rate limits, credential/revision
  changes) with exact-scope durable records, typed browser session
  contracts (stale targets refused; a click is never completion), and
  grounded deliverables (resume additions flagged; report claims
  classified).
- **15.22–15.24 learning**: per-ATTEMPT attribution through Phase 14's
  reward events (a human-supplied resolution never credits the failed
  specialist; branches stand alone); specialization evaluated vs the
  fixed baseline by family (more agents alone proves nothing); learned
  specialist selection through the contextual policy — selection is
  NOT authority, the contract layer still enforces everything.
- **15.25–15.27 workspace**: `/workflows/{id}/delegations` + per-task
  cancel endpoints; the SpecialistPanel (states, budgets, evidence,
  three DISTINCT interventions) and the CoordinationGraph (server-backed
  dependency view, motion policy-gated, usable without WebGL); progress
  counters derive from authoritative task states (vitest-proven).
- **15.28–15.30 release**: Firestore delegation surface completed to
  parity (pending_contracts/subtree/equivalent-work); the flagship
  self-extension journey end to end; SPECIALIST_STAGES release flags
  (read-only default) with operator role-disable and visible role
  versions.
- Full suite: **1074 passed, 0 failed, 48 skipped**; frontend vitest
  50/50, tsc clean.

## v33.0.0 — 2026-11-04

### Phase 14 — every subphase 14.0→14.39 implemented with its own named acceptance row

v32 closed the audit findings; v33 gives EACH subphase a dedicated,
traceable implementation + acceptance test (nothing rests on
"covered by an earlier phase"):

- **14.0 evidence register**: acceptance claims are queryable rows bound
  to the test that proves them — `verified_locally`/`integrated` rows
  WITHOUT a test reference are refused by construction.
- **14.1 release scope**: the contextual selector is a release-scoped
  stage (`ROUTING_STAGE_ENABLED` or `IMPROVEMENT_STAGES_ENABLED=routing`)
  — off by default; the documented degradation is the baseline global
  ranking, logged honestly as `global_rank_fallback`.
- **14.2 measured summary**: `/usage/routing/summary` — per task
  category: verified quality with CI95, cost, latency, confidence, and
  EXPLICIT missing-feedback counts (both stores).
- **14.3 job recovery**: dedicated crash-window tests (create-or-get
  identity, lease takeover, reconciliation, bounded exhaustion, drain
  ownership).
- **14.4 capability gates**: the worker's gate composition tested —
  generation success alone never resumes a parent; the ladder is
  ordered.
- **14.5 browser checkpoints**: durable, monotonic checkpoint
  references on the browser session after every successful executed
  action; recovery reads the reference, never a claim.
- **14.6/14.7/14.8**: comparison-pipeline contracts (create-or-get,
  per-case resume, no fabrication); durable activation receipts;
  learning ownership as an AST guard (the runtime writes stats ONLY
  through the sanctioned reward contract).
- **14.9–14.13**: the routing action catalog (visible availability +
  reasons), eligibility as one filter (pinned-but-unavailable is a
  refusal, never a substitution; the gemini client derives candidates
  FROM the catalog), a validated context schema (known categories,
  non-negative sizes, probabilities in [0,1] — enforced at
  persistence), full-field decision-record persistence, and an AST
  guard that exactly ONE selection site exists.
- **14.14–14.19 confirmations + parity**: probability distribution sums
  to 1; exploration admission as one atomic transaction; ONE reward
  specification shared by every learner; SQLite/Firestore method
  surfaces now MATCH for routing, stats, and job repos (the Firestore
  routing repo gained reward events, policy markers, and summaries).
- **14.23–14.25**: fixed-baseline comparison by category; CI95 on the
  IPS aggregate with honest support flags; the policy ARTIFACT —
  global + per-context state captured and restored EXACTLY (rollback
  reproduces the policy, not just the leaderboard).
- **14.26–14.29**: measured shadow observations (not labels), the hard
  canary cohort cap + predeclared fraction, deterministic variant
  assignment, and a REAL drift fix — the "recent" bucket is now
  COUNT-rotated (last ≤20 outcomes) on both stores; the old daily
  rotation let a day of healthy traffic mask a recent collapse.
- **14.30–14.33**: recovery credit only for VERIFIED repairs; revision
  proposals start candidate_unavailable; browser recovery levels are
  honest (invalid storage state is NOT reconstruction material);
  specialist contracts carry the measurement fields evaluations join on.
- **14.35/14.36**: the frontend's first vitest suite (14 tests) —
  server-derived outcome truth, explicit state labels, motion tiers;
  the 3D scene's zoom clamp is ONE tested contract shared by wheel and
  pinch (anchored to the gesture-start zoom).
- **14.37 flagship journey**: the complete vertical — catalog →
  eligibility → release-scoped selection → decision log → durable
  evaluation → reward event → atomic dual consumption → summary →
  drift → idempotent replay — in one test against production stores.
- **14.38 release evidence**: RELEASE_EVIDENCE.md gains the Phase 14
  packet; the evidence register drives the claim (blocked work is
  visible, proving rows name their tests).
- Full suite: **1012 passed, 0 failed, 48 skipped**; frontend vitest
  45/45, tsc clean.

## v32.0.0 — 2026-11-04

### Phase 14 complete: verified contextual routing + controlled learning

All audit findings A–K closed and the 39 subphases (14.0–14.39) landed:

- **14.A/14.B (selection honesty)**: the epsilon-mixture probabilities
  were wrong (P(top) recorded 1.0) — corrected to the complete
  distribution P(top)=1−ε=0.90, P(other)=ε/(n−1); exploration admission
  is now a single atomic count-and-claim transaction on both stores
  (concurrent selectors cannot exceed the budget; storage failure
  degrades to the baseline, never to unbounded exploration).
- **14.C/14.D (durable rewards)**: rewards became immutable EVENTS with
  the unique identity (decision, evaluation, reward_version). Each
  learner consumes through ONE transaction that applies the stats
  update and the consumption mark together — crashes neither lose nor
  duplicate learning; the global leaderboard and the contextual bandit
  consume independently (separate columns). The worker's parallel
  re-derivation was removed (one reward path, not two).
- **14.E/14.22 (supported estimator)**: the offline comparison no longer
  presents a biased direct-method number as a policy value — the
  replay is split into the propensity-supported IPS term (with
  degeneracy flags: a deterministic log supports NO counterfactual
  claim) and an explicitly DESCRIPTIVE replay; the API names its
  estimator and what each number may claim.
- **14.F (Firestore learning contract)**: the full learning contract
  now exists in Firestore parity — per-context stats, transactional
  exploration admission, drift windows, and atomic reward-event
  consumption (all reads/writes inside the transaction).
- **14.G (job fencing)**: evaluation-job `complete`/`fail` now REQUIRE
  the claim identity and CAS on owner + live lease + state version —
  an owner-less call is a contract error, not a bypass; the reconciler
  may requeue only ownerless work; takeover is the expired-lease
  recovery path. Both stores.
- **14.H (undeclared ≠ empty)**: the orchestrator's build tool accepts
  and propagates the operation declaration into the build row, the
  build task input, AND the command payload; the readiness gate now
  refuses a TASK-originated build whose declaration is empty (an
  API-originated explicit [] stays the operator's choice).
- **14.I (authoritative browser state)**: browser postconditions verify
  against the RUNTIME-captured observation (`browser_evidence`:
  observed URL/title/element digest at the done decision) — a model's
  narrative claiming the postcondition without runtime evidence is
  UNVERIFIABLE, never a pass.
- **14.J (comparison integrity + crash-safe activation)**: the
  comparison enqueue now hash-gates the submitted cases against the
  dataset pinned at proposal (a swapped case set can never become
  promotion evidence); the activation receipt is DURABLE (pending
  commits before the pointer move) — a crash between promotion and
  activation is reconcilable by identity, and an EFFECTIVE receipt is
  returned as-is (never double-applied).
- **14.K (frontend receipt consumption)**: the experiments page
  consumes the activation receipt — the UI shows what actually
  happened to the runtime (effective/failed/reconciled + version),
  not just the experiment state label.
- **14.20 (cost dimension)**: the reward event carries measured cost;
  contextual stats track an EWMA cost and the score uses it as a
  tie-breaker with LESS weight than latency — and only from MEASURED
  cost (a missing measurement is never a bonus). First-sample EWMA
  seeding fixed on both stores (the first observation previously
  double-applied through a self-blend).
- **14.21 (quarantine)**: malformed reward events (invalid value/model)
  are marked consumed WITH a visible quarantine reason — they cannot
  poison the learner loop or reprocess forever, and never become
  learning.
- **14.34 (learning dashboard)**: the operations page gained the
  routing-log health panel (decisions / verified feedback / explicit
  missing-feedback / distinct actions) beside the drift table.
- **14.39 (RL research lane)**: the production/research boundary is
  enforced by a guard test — the execution path imports no
  research-lane module and every learning write enters through the
  sanctioned reward-event contract.
- Full suite: **954 passed, 0 failed, 48 skipped**; frontend tsc clean.

## v31.0.0 — 2026-11-04

### Phases 7–21 deep audit: every named suite verified

The row-by-row audit extended to the remaining phases:

- **Phase 7**: content-hash binding proven (altered code →
  DENIED_CAPABILITY_REPLAY; foreign-tenant replay → denied); dependency
  locks enforce EXACT hashes with extra-dep and missing-dep rejection
  (36 suite tests + live probes).
- **Phase 8–11**: 112 tests across browser sessions, DOM targeting,
  verified ReAct, self-healing, and chaos recovery — all green.
- **Phase 16–21**: 100+ tests across memory lifecycle, deliverable
  verification, evaluations/experiments APIs, improvement
  governance/stage flags, and the automation inbox — all green.
- Full suite: **916 passed, 0 failed, 48 skipped**; frontend tsc clean.
- The roadmap's 22 phases are now audited: Phases 1–6 and 12–15
  row-by-row with named-defect closures; Phases 7–11 and 16–21 by
  their acceptance suites; the deployment-gated items (staging drills,
  real-browser fixtures, the flagship benchmark, SSO/marketplace)
  remain BLOCKED/EXCLUDED in the ledger.

## v30.0.0 — 2026-11-03

### Phases 3–6 deep verification: the second-pass acceptance criteria

- **Phase 3 §3.11/§4.10 (real bug)**: the capacity lease `renew`/`release`
  WHERE clauses used `(worker_id='' OR worker_id=?)` — a lease with an
  empty stored worker_id accepted ANY caller's renewal (an imposter
  could extend another execution's lease). Fixed on BOTH stores: strict
  `worker_id=?` equality (empty-id only matches empty-caller); Firestore
  `_verify_renewable` + release gained the same check. Probes: imposter
  renewal rejected, empty-id legacy compat preserved, stale-generation
  renewals rejected.
- **Phase 4 §4.16 (real defect)**: the webhook replay key was consumed
  by `atomic_insert` BEFORE command acceptance, with NO cleanup on
  failure — a queue-full rejection permanently burned the sender's
  retry identity. Fixed: `release_replay_key` (SQLite + Firestore)
  releases running/paused entries on acceptance failure; ACCEPTED
  deliveries are never released. The webhook router releases the key in
  both failure paths (RuntimeError + receipt.accepted=False).
- **Phase 5/6**: the second-pass acceptance criteria verified by their
  suites (effect dispatch 16, uncertain reconciliation 6, connector 39,
  phase6 22) — the §5.11 CAS guard, §6.5 canonical identity, §6.7
  credential bindings, and §6.12 eligibility evaluator all hold.
- Tests: test_phase3_6_deep.py (5). Full suite: **916 passed, 0 failed,
  48 skipped**; frontend tsc clean.

## v29.0.0 — 2026-11-03

### Deep audit closure: Phases 1, 3, 4, 5, 6 — the roadmap's named defects

A row-by-row audit against the detailed Phase 3 (3.0–3.23), Phase 4
(C1–C16/4.0–4.27), Phase 5 (5.0–5.23), and Phase 6 (6.1–6.24) roadmaps,
closing the remaining named defects:

- **Phase 1 — real bug fixed**: `accept_invitation` crashed with a naive
  vs aware datetime `TypeError` on a malformed/legacy expiry — a 500
  instead of a clean rejection, on BOTH stores. Hardened: unparseable or
  naive expiry → invitation marked expired + a clean ValueError (§1.7).
- **Phase 3 — verified**: 76 tests across the named suites
  (test_distributed_rate_limits, test_capacity_leases,
  test_queue_backpressure, test_admission_policy,
  test_schedule_admission, test_webhook_admission); the §3.0
  cleanup-unit and zero-limit defects stay fixed; bounded local queues
  with the no-deadlock delayed-retry store; command admission wired
  into workflow/webhook/build routes (§3.10).
- **Phase 4 — C12 closed**: the three SQLite-specific `repo.db.execute`
  calls (deadline-expiry sweep, cancellation SLA escalation, command
  event history) replaced with backend-neutral repository methods on
  BOTH stores (`find_deadline_expired`,
  `find_unacknowledged_cancellations`, `expire_command`,
  `command_events`); plus the transition matrix gained RUNNING→EXPIRED
  (§4.3: the allowed lifetime can end mid-run).
- **Phase 5 — §5.11 gap closed**: the legacy `reconcile_uncertain` had
  NO conditional state guard (two concurrent resolvers could both win) —
  now an owned transaction with a `WHERE status='uncertain'` CAS update;
  the loser raises instead of overwriting history.
- **Phase 6 — verified**: 39 tests across the connector suites; the
  canonical revision identity (sorted ops, mutable evidence excluded),
  credential bindings with `authorization_epoch`, and the eligibility
  evaluator all in place from the earlier Phase 6 rounds.
- Full suite: **911 passed, 0 failed, 48 skipped**; frontend tsc clean.

## v28.1.0 — 2026-11-03

### Phase 2 deep verification: the acceptance criteria behind the matrix

Beyond the row-by-row audit: the §2.17 acceptance criteria that had no
dedicated test now do (test_phase2_deep.py, 11 tests):

- **Overrun**: actual usage exceeding the hold is recorded UNCLIPPED
  and surfaced in the budget event trail (`overrun: true`); the real
  consumption constrains subsequent admissions.
- **Crash window**: a reservation stuck in `dispatching` preserves its
  hold and lists as a RECONCILIATION CANDIDATE — expiry triggers
  investigation, never unconditional release (§2.6).
- **Uncertainty**: uncertain reservations stay COUNTED against the
  allowance until resolved; reconciliation can settle them.
- **Replay/conflict**: settlement replay is idempotent; a conflicting
  receipt on a settled reservation raises SETTLEMENT_CONFLICT; settling
  a nonexistent reservation is an error, never unexplained consumption.
- **Release idempotency**: double-release removes the hold exactly once.
- **Fallback attribution**: a fallback provider attempt gets its own
  distinct reservation (§2.11).
Full suite: **911 passed, 0 failed, 48 skipped**.

## v28.0.0 — 2026-11-03

### Phases 1–2 subphase audit: every roadmap row verified (1.0–1.18, 2.0–2.21)

Audited the two most detailed original roadmaps subphase-by-subphase and
closed the explicit matrix gaps:

- **§2.0 original defects re-probed FIXED** against the current tree:
  fresh-row cap enforcement (150/100 → rejected), duplicate settlement
  (idempotent, settled once), independent-connection visibility.
- **Phase 1.15 gap closed**: re-adding an existing member with a
  stronger role is REFUSED by the shared invitation service ("Already a
  member; use the owner-only role endpoint") and the existing role is
  unchanged — stronger than the roadmap's minimum bar.
- **Phase 2.14 gap closed**: the accounting period is UTC
  (`utc_period_day`); settlement charges the ADMISSION period even when
  the response crosses midnight.
- **Phase 2.17 gap rows closed**: negative reservation → validation
  failure; zero allowance rejects positive-cost calls; a reduced limit
  preserves the settled history while new admissions respect the tighter
  policy.
- **Phase 2.3 semantics verified**: null inherits the configured
  default, 0 blocks new spend, negatives and above-ceiling values
  rejected.
- Provider-boundary coverage confirmed: every paid path (gemini, groq,
  huggingface, embeddings, resume/jd generation) routes through
  `_metered_attempt` / the budget service.
- Tests: test_phase1_2_matrix_gaps.py (7). Full suite: **900 passed, 0
  failed, 48 skipped**.

## v27.0.0 — 2026-11-03

### Phase 15 — specialized subagents (the roadmap's named files)

- **`backend/services/agent_task_contracts.py`** (the roadmap-named
  integration service): re-exports the contract primitives and provides
  the SPECIALIST_REGISTRY — 7 roles, each with its title, AUTHORITY
  BOUNDARY, runner path, and typed output schema. `delegate_specialist`
  applies the registry's schema by default (narrowable, never widenable
  past the role's boundary); unknown roles refuse.
- **`backend/agents/verifier/verifier_agent.py`** (§15.1): the Outcome
  Verifier — evidence-only (output + criteria + effect/artifact
  references), NO tools, strict `VerificationVerdict` output. Runs
  through a bounded contract (dedup per run+task, typed output
  validation, lease-fenced claims). The §12.19 combination rule holds
  at the contract boundary: a deterministic FAIL stands even when the
  judge claims pass (proven with an optimistic-judge test); without a
  model client it completes HONESTLY with the deterministic verdict
  (never fabricated).
- **`backend/agents/connector_validator/validator_agent.py`** (§15.2):
  the Connector Validator — proposes operation-specific test cases
  (auth denial, required params, pagination, malformed responses,
  controlled writes) through a bounded contract; the §6 pipeline
  executes and records the ACTUAL outcomes — the agent cannot invent
  passing results. Without a model it proposes the deterministic
  baseline case set. ADK-style event outputs normalized by a shared
  parser.
- **Engine delegation (§15.1 live path)**: when the deterministic
  verification lands needs_review, the engine delegates to the Outcome
  Verifier specialist through a bounded contract (best-effort — the
  deterministic verdict stands when the specialist is unavailable; the
  combined verdict never upgrades a fail).
- Tests: test_phase15_agents.py (8 — registry coverage, deterministic
  fallback, fail-stands-over-judge-pass, dedup (deterministic create
  race), baseline cases, model proposals recorded, registry-driven
  delegation, unknown-role refusal). Full suite: **893 passed, 0
  failed, 48 skipped**; frontend tsc clean.

## v26.0.0 — 2026-11-03

### Phase 14 — contextual routing and advanced reinforcement learning

**Prerequisite verified first**: Phase 13's completeness audit — all 33
subphases with terminal statuses, findings A–L closed, 81 Phase 13
evidence tests green — before starting Phase 14.

- **Contextual bandit** (`services/contextual_policy.py`): a
  context-bucketed UCB1 over VERIFIED outcomes (category × capability
  class × input-size bucket, per-bucket × model stats in the new
  `contextual_model_stats` table) — the fix for the non-contextual
  router's core flaw (a model credited for research successes is NOT
  credited for browser failures; proven by the biased-rewards test).
- **Stochastic exploration with LOGGED probability** (§14.4): the
  epsilon-mixture logs its exact mixture probability (eps/(n-1) for
  explored alternatives, 1.0 for greedy) — accurate propensities make
  the offline comparison valid (§13.6's honest-mechanism rule carries
  over: the mechanism field names what actually ran).
- **Filtering first** (§14.3): candidates arrive pre-filtered by
  permissions/provider configuration — the policy never sees a
  forbidden action, so exploration cannot discover one.
- **Bounded exploration budget** (§14.5): durable windowed event log
  (`exploration_events`, 200/window); when exhausted the policy
  degrades to GREEDY — never to off-list actions or unbounded
  exploration.
- **Drift monitoring** (§14.5): recent-window vs long-run mean reward
  per context with a >0.25/≥20-samples drift signal — reported via
  `/usage/routing/drift` and the operations-page Routing Drift panel;
  the policy does NOT auto-tune on it, and security constraints stay
  OUTSIDE the scalar reward.
- **Engine wiring**: the contextual selection runs before the legacy
  rank (re-ordering the candidate chain), logs the exact
  mechanism+probability, and the worker's feedback join now feeds BOTH
  the global and the per-context stats from the same verified outcome.
- Fine-tuning / multi-step RL stays EXCLUDED (roadmap §14.6 — separate
  design, consented data, safe environment).
- Tests: test_routing_policy.py (11 — the roadmap's named file:
  delayed/missing feedback, availability changes, biased rewards,
  drift, repeated failures, budget exhaustion, probability logging,
  filtering, offline comparison). Full suite: **885 passed, 0 failed,
  48 skipped**; frontend tsc clean.

## v25.0.0 — 2026-11-03

### Phase 13 COMPLETE: all 33 subphases (§13.0–13.33)

The remaining subphases, closing the controlled self-improvement loop
end to end:

- **§13.10 immutable experiment manifests**: every referenced artifact
  pinned at proposal (prompt version ids + CONTENT hashes, dataset
  hash, evaluator/rubric versions, the typed metric protocol);
  nonexistent/unrelated refs REJECTED at proposal; content mutation
  under a pinned version forces a NEW experiment (promotion verifies);
  deleted versions fail visibly at activation.
- **§13.11 versioned datasets**: development / held-out / adversarial
  sets with provenance; the held-out set requires ALL six case families
  (supported successes, unsupported tasks, missing credentials,
  ambiguous outcomes, malformed outputs, factuality failures); it is
  DRAFT-PROOF (only approve-capable principals modify it) and is used
  VERBATIM at evaluation — the drafting agent cannot tune its own
  judge.
- **§13.18 evidence before exposure**: predeclared
  `min_shadow_observations` / `min_canary_exposures` gate canary entry
  (measured observations, not labels) and promotion (counted cohort
  admissions) — failed offline evidence cannot enter a canary and
  promotion cannot reuse offline numbers.
- **§13.22 measured recovery**: recovery measured by failure class from
  durable episodes — verified recovery rate, repeat-failure rate;
  'repair queued'/'retry attempted' NEVER count as healed.
- **§13.23 connector-revision improvement**: candidates proposed from
  reviewed failures via the §6.2 checkpoint machinery, permission
  expansion REFUSED, candidate stays UNAVAILABLE until the §13.3
  readiness gates pass.
- **§13.26/§13.27**: the shared run-event layer has a REAL consumer
  (the workspace page reconciles snapshot-first, merges the reconciled
  run); the 3D scene is labeled `data-illustrative` with an accessible
  aria-label — decorative motion never impersonates telemetry.
- **§13.28**: the review's specialist table delegates through the SAME
  bounded contracts (evaluation curator, improvement proposer, release
  verifier, reconciliation) — boundaries hold per role.
- **§13.29 deliverable verification**: existence + ownership +
  content-addressed checksum + format + factuality (§17.3 provenance
  integration) — a download link alone never verifies; unsupported
  facts force review.
- **§13.31 governance**: drafting/comparison/shadow calls meter through
  the durable usage ledger; evidence redaction (raw payloads and
  credentials → [REDACTED]); shadow-observation retention purge;
  queue/open-experiment bounds.
- **§13.32 evidence levels**: the level-3 API→worker→runtime test (API
  evaluate → comparison worker → API promotion → effective runtime
  change) plus the ledger's level-1/2 pointers; level 4 (staging)
  remains explicitly excluded from local claims.
- **§13.30 THE FLAGSHIP JOURNEY**: one recorded vertical — missing
  capability identified → durable build with required operations +
  credential reference → account binding → operations TESTED against
  the checkpoint revision → REAL §6.14 atomic activation → parent
  resumed via the authoritative readiness gate → bounded recovery with
  a VERIFIED episode → deliverable independently checked → evidence in
  evaluation records + recovery metrics + inbox surfaces.
- **§13.33 progressive rollout**: stage flags (evidence always on;
  proposals/comparisons/shadow/canary/activation each gated) wired into
  the API — a deployment enables the loop stage by stage and can stop
  it at any stage.
- Fixed en route: the comparison worker now advances the experiment
  PROPOSED→EVALUATED with its metrics (no permanent EVALUATING), the
  readiness gate reads both auth shapes, and the conftest resets ALL
  Phase 5–13 singletons (systemic cross-test pollution fix).
- Tests: test_phase13_chat7.py (12), test_phase13_chat8.py (6),
  test_phase13_chat9.py (4 — flagship + evidence levels). Full suite:
  **874 passed, 0 failed, 48 skipped**; frontend tsc clean.

## v24.0.0 — 2026-11-03

### Phase 13 final chat: live browser validator · durable comparisons · measured shadow · principal authority

Closes the remaining Phase 13 review findings (D and E) and the
§13.9/§13.14–13.16 subphases:

- **D (§13.4)**: the FULL browser-action validator (ownership, session
  state, controller, staleness, origin) runs before EVERY live dispatch —
  not just argument shape. Consequential classification extends beyond
  form submission to deletion/publish/transaction controls (keyword
  classification over the control's accessible identity) and
  confirm/checkout URLs. A consequential action whose effect record
  CANNOT be persisted is STOPPED — never dispatched behind a debug log
  (the refusal lands in the step result).
- **E (§13.13)**: the unconditional 501 is replaced by a durable
  comparison pipeline — create-or-get job identity per pinned revision,
  resumable per-case records (a crash midway resumes without rewriting
  completed evidence), lease-fenced completion, §13.12-shaped metrics
  (samples, success, constraint flags derived from case details). The
  evaluate API returns a job identity with pinned cases and refuses
  invented inputs (422); the worker drains comparisons in its
  housekeeping loop (executor deployment-wired; nothing fabricated).
- **§13.9**: proposal diffs are unified diffs over the ACTUAL
  baseline/candidate contents; equivalent proposals (same candidate
  version) deduplicate.
- **§13.14**: the effective prompt version persists on the execution
  step (`policy_revision`) per attempt — receipts prove which template
  ran; an experiment-assigned candidate overrides a stale
  `_instructions`, while explicit task instructions win when no
  experiment applies.
- **§13.15**: trusted `Principal` gates (operator/agent/system with
  capability sets) replace actor-name-prefix checks across every
  improvement-service entry point; legacy strings parse conservatively
  (agent:* ⇒ propose-only).
- **§13.16**: measured shadow — every candidate execution on a SHADOW
  experiment records an observation (outcome, latency, token cost);
  `shadow_summary` reports the distribution; 'shadow observed' requires
  recorded candidate execution, never a state label.
- Tests: test_phase13_chat6.py (5), test_experiments_api.py updated to
  the new evaluate contract, 13.4 acceptance tests in chat1 (10 total
  there). Full suite: **852 passed, 0 failed, 48 skipped**; frontend
  tsc clean.

## v23.0.0 — 2026-11-03

### Phase 13 review round: execution evidence, durable delivery, authoritative gates, effective activation

Corrects the second Phase 13 audit's confirmed findings A–L (except the
two explicitly deferred: D — live browser validator wiring, E — the
comparison worker — both recorded in the ledger as next-chat work):

- **A (§13.1)**: engine steps traverse the REAL §10.6 lifecycle with
  legal actors (the old OBSERVING→READY-by-"engine" call violated the
  matrix twice and was swallowed); the validated-arguments digest lands
  at DISPATCH (the tool-call metric's basis); lost leases SUPERSEDE
  (never counted); duplicate delivery cannot inflate. Plus a latent
  NameError fix (module-level `time` import).
- **B (§13.2)**: crash-safe evaluation delivery — `claim_one` (the
  terminal hook claims ITS run's job; no stranding), an independent
  bounded drainer in the housekeeping loop, lease-owner FENCING on
  completion (a stale worker cannot overwrite), visible
  `exhausted`/`reset_exhausted`, reconciliation for un-enqueued
  terminals, and the Firestore parity repository.
- **C (§13.3)**: connector readiness is AUTHORITATIVE —
  `required_operations` travels through the durable build contract
  (undeclared coverage is refused); readiness demands PASSING,
  unexpired, current-revision, credential-bound validation runs.
- **G (§13.8)**: the proposer consumes a real `reviewed_failures` query
  (terminal, tenant-scoped, time-windowed) — active abandoned episodes
  are recovery candidates, never improvement candidates.
- **I (§13.12)**: typed promotion gates — finite numeric metrics, real
  booleans, required sample counts, direction-aware regression, and
  SERVER-OWNED mandatory constraints (unauthorized actions, unresolved
  effects, unverified success cannot be omitted by a proposal).
- **F (§13.19/§13.20)**: the deployment coordinator — API promotion
  ACTIVATES the candidate with a reconcilable receipt
  (pending→effective/failed); API rollback supplies the pinned-baseline
  restore, refuses overwriting a newer release, and reports
  rollback-CONFIRMED only when the effective version actually reverted.
- **H (§13.17)/J (§13.21)**: durable `experiment_exposures` enforce the
  EXACT absolute cohort cap; active experiments resolve by target query;
  Firestore activation verifies version ownership; the coordinator
  honors expected-active-version guards.
- **K (§13.6)**: routing feedback identity — the selection MECHANISM is
  recorded (deterministic ranked first-fit, probability 1.0, never
  labeled uniform random); fallback rewards go to the RESOLVING model
  only with MEASURED latency; policy application is idempotent via a
  per-decision marker.
- **L (§13.25)**: frontend corrections — rollback prompt cancellation
  performs no mutation; canary size = the predeclared cap; no hardcoded
  population; deployment identity derives from the activated version.
- Tests: test_phase13_chat1.py (7), test_phase13_coordinator.py (5), 10
  readiness/typed-gate/feedback tests across existing files; parity
  21/21 on a live emulator. Full suite: **842 passed, 0 failed, 48
  skipped**; frontend tsc clean.

## v22.0.0 — 2026-11-02

### §12.19 model judges · §21.3 run replay · §12.5 recovery outbox intents · §12.30 pagination

- **Model judges (§12.19)**: judges assist ONLY with judge-eligible
  criterion kinds (`semantic_match`, `readability`) — the verifier marks
  those needs_review (awaiting the judge), never a hard fail and never a
  silent pass. Authoritative kinds (artifact/effect/http) are REFUSED
  outright: a judge's confident explanation cannot validate a
  nonexistent artifact. Every judge call records model, rubric version,
  and a prompt digest; the combination rule means a judge's pass can
  never upgrade a deterministic fail; unusable output and judge absence
  stay needs_review. The labeled-example harness includes an
  ADVERSARIAL case (injection inside the output) — a rubber-stamping
  judge fails the harness, gating judge use in evaluations.
- **Run replay (§21.3)**: `replay_run` re-applies the CURRENT policy to
  a run's durable execution record — which recorded steps would
  revalidate, the historical evaluation reported as historical. Pure
  inspection: no dispatch, no mutation, no effect repeated.
- **Recovery outbox intents (§12.5 "durable transitions AND outbox
  intents")**: transitions into PROPOSED / ESCALATED /
  VERIFIED_RECOVERY / RECOVERY_FAILED commit an outbox event IN THE
  SAME transaction (SQLite + Firestore parity) — a crash between the
  transition and any publication loses nothing.
- **§12.30**: evaluations retrieval is bounded with STABLE ordering
  (created_at_epoch, evaluation_id) in both stores, and each record
  carries `evidence_completeness` (gaps, unattributed effects,
  measurement quality) — §12.18's missing-evidence visibility in the
  API.
- Tests: test_round3_judges_replay.py (9); Firestore parity 21/21 on a
  live emulator. Full suite: **816 passed, 0 failed, 48 skipped**.

## v21.0.0 — 2026-11-02

### §12.11 durable evaluation jobs · §21.6 automation inbox · §21.3 run comparison · §12.17 family evaluators · §19.6 motion policy

- **Durable evaluation jobs (§12.11 closure)**: terminal events enqueue
  evaluation work through an `evaluation_jobs` table with create-or-get
  identity (duplicate terminal events return the SAME job), CAS lease
  claims, bounded requeue with visible `exhausted` parking, and the
  worker now runs evaluations THROUGH the job (crash-safe: failures
  requeue, health is queryable).
- **Automation inbox (§21.6)**: one actionable surface aggregating
  pending approvals, failed runs, NEEDS_REVIEW runs (§12.2), and
  uncertain external outcomes — severity-ordered, each item with the
  precise next action and link; another tenant's items never appear.
  `GET /api/v1/inbox`.
- **Run comparison (§21.3)**: evidence-based comparison of two runs
  (`GET /evaluations/compare/{a}/{b}`) — success/task/recovery/tool/cost
  deltas with comparability flagged by fixture hash; read-only, the
  runs' external effects are never re-executed.
- **Task-family evaluators (§12.17)**: the registry maps the product's
  actual families (api_read / api_mutation / browser_task /
  research_report / resume / connector_build / recovery / code_project)
  to mandatory deterministic checks — an api_mutation REQUIRES provider
  evidence (`effect_resolved`), never a non-empty reply; checks are
  deterministic per family and unknown families are refused.
- **Motion policy (§19.6)**: one source of truth
  (`lib/motion.ts`) for full/reduced/static tiers — decorative motion
  off on `prefers-reduced-motion` and data-saver, essential state
  transitions kept except in `static`.
- Tests: test_phase18_21_round2.py (10). Full suite: **807 passed,
  0 failed, 48 skipped**; frontend tsc clean.

## v20.0.0 — 2026-11-02

### Phases 00/18/20: release scope, resilient frontend state, operational health

**Phase 00 — product contract and reproducible verification:**
- [docs/release-scope.md](docs/release-scope.md): the supported journeys
  with fixtures + expected outcomes, the EXPLICIT exclusion list, the
  supported technical scope, and product-wide quality measures with
  thresholds set BEFORE measuring candidates (§00.5).
- `scripts/verify-local.sh` extended: source-identity header (commit +
  dirty-file count), fresh-database migration check (18 required
  tables), and the emulator-gated Firestore parity stage.

**Phase 18 — React architecture and state consistency:**
- **Shared run/outcome state model** (`lib/task-states.ts`): the
  execution lifecycle gains NEEDS_REVIEW/VERIFYING/WAITING, and the
  GOAL OUTCOME is a separate derived state (verified / partial /
  uncertain / needs_review / pending_evaluation / unavailable) mapped
  from the durable evaluation — the UI never upgrades a claim the
  evidence lacks (§18.1 + §12.3).
- **`useRunEvents` hook (§18.2)**: snapshot-first reconciliation on
  mount and every reconnect (missed events cannot leave stale UI),
  stale-run rejection after navigation, terminal runs settle without
  streaming, re-sync on reconnect.
- **Draft preservation (§18.3)**: `useDraft` (sessionStorage per
  thread) wired into the composer — typed work survives refresh and
  auth expiry; the persisted draft clears on successful send.
- **Request behavior (§18.5)**: `apiFetch` carries a 30s default
  timeout with a typed timeout message.
- §18.4 (two-tab refresh-token rotation) was already implemented via
  Web Locks in `lib/auth-session.ts`.

**Phase 20 — observability (§20.1/§20.2):**
- Request correlation was present (X-Request-ID middleware); added the
  `/admin/operational-health` aggregate: queue age, stuck leases,
  uncertain effects, outbox backpressure, overdue commands, stale
  recovery leases, browser-session sweep candidates, and the routing
  log's feedback coverage — every threshold that pages an operator,
  visible before it pages. Admin-gated.

Tests: +1 operational-health. Full suite: **797 passed, 0 failed,
48 skipped**; frontend tsc clean.

## v19.0.0 — 2026-11-02

### Phases 14–17 + 21: contextual routing, subagent contracts, memory lifecycle, deliverable provenance, templates

**Phase 14 — contextual routing with verified rewards (§14.1–14.7):**
- **Routing decision log** (`models/routing_decision.py`, `repositories/
  routing_repository.py` + Firestore parity + `routing_decisions`
  table): append-only decisions carrying the context (§14.2: category,
  capability, input size), the ELIGIBLE set (permission-filtered,
  §14.3), the ranked choice with the HONEST selection probability
  (1/|eligible| under the logged first-fit policy), policy version, and
  provider-fallback lineage (§12.15).
- **Verified delayed feedback (§14.1)**: the learned reward now joins
  from the durable evaluation via `attach_evaluation_feedback` —
  verified→positive, rejected→negative, partial/uncertain tracked in
  the log but NEVER a policy update (an unverified completion cannot be
  a positive reward, §12.35). Feedback joins EXACTLY once (redelivery
  cannot rewrite); missing feedback is explicit, never zero. The
  engine's immediate payload check no longer feeds the policy.
- **Offline policy evaluation** (`offline_policy_evaluation.py`):
  logging-policy value per action/category with CI95 + support flags;
  candidate replay is coverage-aware — unsupported picks are flagged,
  never silently averaged; candidates cannot select outside the
  eligible set; fixed-baseline comparison (§14 acceptance).
- **API**: `/usage/routing/health` (log coverage), `/usage/routing/
  compare` (admin, read-only).

**Phase 15 — specialized subagents (§15.1–15.6):**
- **Agent task contracts** (`models/agent_task.py`,
  `repositories/agent_task_repository.py` + Firestore parity +
  `agent_tasks` table): parent, typed inputs, output SCHEMA (completion
  is refused without the required keys), allowed tools, budget,
  deadline, ownership lease, cancellation.
- **Authority boundaries (§15.1–15.3)**: per-role tool whitelists — a
  verifier holds evidence tools only; requests for tools outside the
  role's authority are REFUSED, never trimmed.
- **Bounded delegation (§15.5)**: depth cap, per-parent fan-out,
  concurrent-children cap, cumulative spend, and cycle defense — all
  enforced inside the creation transaction; duplicate assignments dedup
  via a partial unique index / Firestore dedup anchor.
- **Partial failure (§15.6)**: completion verdicts (require_all /
  require_any / best_effort) name EXACTLY which children failed —
  incomplete results are never silently accepted. Claim races produce
  one winner; deadlines time out; cancellation propagates to active
  children.

**Phase 16 — memory lifecycle (§16.1–16.7):**
- Five memory classes (legacy types map through); provenance (source,
  confidence, expiry) with expiry honored at READ time; retrieval
  authorization is ownership-first (identical embeddings never leak
  across tenants); conflicts SUPERSEDE with preserved, inspectable
  history (never silently merged); user control (inspect, correct into
  a new version, export, forget); deletion propagation keeps
  supersedence coherent and reports derived refs; retrieval quality
  fixture-tested. API: `/memory/export`, `/memory/{id}/correct`,
  `/memory/{id}/forget`.

**Phase 17 — deliverable provenance (§17.3/§17.7):**
- `services/provenance.py`: every significant fact traces to a supplied
  source — exact containment OR ≥80% word overlap (rephrasing stays
  supported, ADDED claims like a fabricated skill are flagged);
  source revisions + content checksums recorded.

**Phase 21 — templates + dry runs (§21.1/§21.2):**
- Versioned workflow templates (immutable versions, active pointer) +
  `/templates` API; DRY RUN: side-effect-free proposal naming expected
  external effects and a clearly-labeled cost ESTIMATE with explicit
  uncertainty — same input twice, same pure plan.

Tests: test_phase14_routing.py (11), test_phase15_subagents.py (15),
test_phase16_memory.py (13), test_phase17_21_deliverables_templates.py
(11); Firestore parity 21/21 on a live emulator (Phase 14/15 stores).
Full suite: **796 passed, 0 failed, 48 skipped**.

## v18.0.0 — 2026-11-02

### Phase 13 rollout: executable self-improvement (§13.1/§13.3/§13.5)

The rollout machinery that makes Phase 13's experiment lifecycle REAL —
versioned stores, a proposer pipeline, deterministic cohorts, and true
rollback:

- **Versioned prompt store** (`services/prompt_versions.py` + Firestore
  parity + `prompt_versions`/`prompt_activations` tables): immutable
  versions with a per-prompt activation pointer; checkout is
  content-addressed (identical content never duplicates); rollback MOVES
  THE POINTER — history is preserved, rows are never rewritten.
- **Policy snapshots** (`policy_snapshots` table + Firestore parity +
  `restore_snapshot` on both adaptive-policy repositories): the routing
  policy's learned counters snapshot durably and restore exactly — the
  learner still never authorizes anything (§12.35).
- **Proposer pipeline** (`services/improvement_rollout.py`): reviewed
  failure clusters (ESCALATED / RECOVERY_FAILED episodes grouped by
  category) become candidate PROMPT experiments proposed by the
  `agent:improvement_proposer` actor — which the ImprovementService
  refuses to let promote (proven in-test). Without a drafting step the
  pipeline proposes NOTHING (no fabricated candidates); proposals are
  bounded (top clusters, dedup against identical drafts).
- **Deterministic cohorts (§13.3)**: shadow/canary assignment is a hash
  of (prompt, subject, experiment) — the same task lands in the same
  cohort across restarts and replicas; shadow NEVER serves the
  candidate; canary honors the predeclared fraction (10k-subject
  distribution test).
- **Task-prompt resolution (§13 rollout)**: tasks carrying `_prompt_id`
  resolve through the store in the engine (fail-open) — the ACTIVE
  version, or the CANDIDATE template when a canary assigns the run to
  the candidate cohort. Read-only rollout per §13: prompts for read-only
  tasks first.
- **True rollback (§13 acceptance)**: `make_restore` wires
  ImprovementService.rollback to the versioned stores — a promoted
  prompt experiment rolls back to its BASELINE version (pointer move) or
  a policy experiment restores its snapshot. Proven end to end: propose
  → evaluate → shadow → canary → promote → activate candidate →
  rollback RESTORES THE PRIOR PROMPT VERSION.
- **API**: admin-gated `/prompt-versions/...` (list/active/checkout/
  activate/rollback); stores wired into the factory for both backends.
- Tests: test_phase13_rollout.py (9), Firestore parity +1 (19/19 on a
  live emulator). Full suite: **746 passed, 0 failed, 46 skipped**.

## v17.0.0 — 2026-11-02

### Review batches 1–2: truthful outcome semantics + durable recovery/build contracts

Implements the reviewed Phase 12 corrections (findings A–I, §12.0–12.8),
with the requirement-and-evidence ledger at
[REQUIREMENT_LEDGER.md](docs/REQUIREMENT_LEDGER.md).

**Batch 1 — truthful ledger and outcome semantics (findings A–E, §12.2–12.4, 12.9–12.16 partial):**
- **Engine completion boundary (A)**: sanitize the NEW result first,
  verify against the task's authoritative `acceptance_criteria`
  (input_data only as legacy fallback), map EVERY verdict to a permitted
  state — `fail`→FAILED, `needs_review`→NEEDS_REVIEW (new task/run
  status; the run can never claim COMPLETED; the owning command parks in
  WAITING, never FAILED), pass→COMPLETED. The verdict + reason persist
  with the task (previously computed but not saved).
- **Runner verdicts gate success (B)**: overall success requires every
  task verifier verdict `pass` plus zero blocking effect interpretations.
- **Explicit identity joins (C)**: effects join tasks by `task_id`/
  `run_id` fields; unattributable effects still gate the run; one task's
  effect can never satisfy another's criterion.
- **§12.4 decision table**: `required_write_interpretation` covers every
  effect state plus UNKNOWN values (→ unverified, never success) and
  mixed multi-target results (PARTIALLY_APPLIED blocks).
- **Authoritative metrics (D)**: pure runner (no draining, §12.10);
  token usage joins the durable usage ledger via
  `usage_summary_for_run` (SQLite + Firestore parity: measured/
  estimated/reserved/uncertain, pricing version, actual `models_used`);
  tool calls = VALIDATED dispatch steps; latency in explicit ms with
  stage timings; unavailable measurements are None (§12.9); successful
  recoveries require verified verdicts.
- **Immutable evaluation history (E, §12.11/12.12)**: create-or-get by
  (run_id, evaluator_version) in BOTH stores (SQLite unique identity
  index + safe rebuild migration; Firestore identity-anchor
  transaction); new scorer versions create LINKED revisions
  (`previous_evaluation_id`); no in-place rewrites.
- **§12.29**: SQLite summary aggregates over the windowed subquery —
  same recent-window semantics as Firestore (window test added).

**Batch 2 — durable recovery and capability-build contracts (findings G–I, §12.5–12.8):**
- **§12.5 (G)**: the recovery EPISODE is the claim authority — atomic
  creation/dedup (SQLite partial unique index on active (run, task) +
  Firestore active-episode anchor with terminal cleanup); CAS ownership
  claims; protected transitions require the CURRENT owner generation and
  a LIVE lease (stale owners cannot commit; both stores); the worker
  refuses recovery without the episode repo and honors the persistent
  absolute deadline (escalates from DETECTED — matrix extended);
  episodes record classification/proposal/escalation; queued repair is
  never reported as recovery success.
- **§12.6 (H)**: organization scope and the credential-BINDING reference
  (never the secret) propagate through the queued build: command,
  build record (new `organization_id`/`credential_reference` columns +
  migration), child run (`org_id`), and task input; the orchestrator
  forwards `api_credential_name` and org context.
- **§12.7 (H)**: the §12.7 readiness ladder (generated → registered →
  credentials ready → required operations tested → eligible →
  activated) gates the parent resume; disabled/unvalidated/unactivated
  connectors park the parent with the precise next action; candidates
  are preserved (no redundant rebuilds).
- **§12.8 (I)**: backend-neutral `initial_work_items` — one typed
  description (parent_wait / build_record / workflow_run_with_task) the
  command service translates to a SQLite callback OR deterministic
  Firestore triples with equivalent create-if-absent semantics.
- **§12.31/F**: EvaluationPanel renders server-derived verdicts, pending
  evaluation vs none, visible retryable fetch errors, effect
  uncertainty, needs-review counts, and refresh — the summary can no
  longer contradict the criterion-level results.
- **§12.0**: [REQUIREMENT_LEDGER.md](docs/REQUIREMENT_LEDGER.md) —
  the full status ladder (PRESENT → INTEGRATED → VERIFIED LOCALLY →
  VERIFIED IN STAGING / BLOCKED / EXCLUDED) across findings and 12.x.
- Tests: test_review_batch1_outcomes.py (16), test_review_batch2_build_
  contracts.py (9), recovery + deadline + dedup suites expanded;
  Firestore parity 18/18 on a live emulator. Full suite: **737 passed,
  0 failed, 45 skipped**.

## v16.0.0 — 2026-11-02

### Phase 13 — controlled self-improvement loop (§13)

- **Experiment model + state machine** (`models/experiment.py`):
  PROPOSED → EVALUATING → EVALUATED → SHADOW → CANARY → PROMOTED /
  REJECTED / ROLLED_BACK / CANCELLED with per-role CAS transitions.
  Separated permissions (§13.4): proposer, evaluator, approver,
  activator, operator are distinct roles; agents ("agent:*") may propose
  but can NEVER evaluate, approve, promote, or roll back, and may never
  propose changes to authorization/grading/permission targets.
- **Durable experiments** (`repositories/experiment_repository.py` +
  Firestore parity + `experiments` table): records survive restart;
  every transition records the acting role + actor; stale versions
  conflict.
- **Improvement service** (`services/improvement_service.py`):
  - §13.1 failure clusters (`failure_cluster_summary`) group durable
    recovery episodes by category to feed candidate proposals, which
    require a stated hypothesis and expected benefit.
  - §13.2 evaluation runs baseline AND candidate on the SAME held-out
    set; the dataset hash and grading hash are pinned at proposal — a
    swapped dataset or changed thresholds refuse to evaluate.
  - §13.3 shadow mode, then a canary cohort bounded by PREDECLARED
    fraction + absolute caps (`check_cohort_fraction` verifies the
    running cohort stays inside the population fraction).
  - Promotion gates, in order: non-agent approver ≠ proposer; cohort
    fraction; success floor; regression ceiling per metric; cost
    ceiling; ZERO critical-constraint violations — a candidate that
    improves AVERAGE quality but violates a critical constraint cannot
    promote (the §13 acceptance gate, with a dedicated test showing
    0.80 → 0.96 success blocked by one violated constraint).
  - Promotion evidence pins dataset hash + results + actor + deployment
    version; rollback records the operator, reason, and restores the
    prior ref through a pluggable versioned store.
- **API** (`api/routers/experiments.py`): admin-gated propose/list/get/
  shadow/canary/promote/reject/rollback; the service enforces the
  permission separation deterministically and the router requires an
  admin principal (two independent layers). Offline evaluation without
  a wired kind-specific evaluator fails closed (501
  EVALUATOR_NOT_WIRED) — the API never fabricates scores.
- **Frontend**: typed helpers + `dashboard/experiments/page.tsx` showing
  lifecycle state, hypothesis, pinned dataset/constraints, verified
  deltas, promotion evidence, and role-appropriate actions.
- Tests: test_improvement_loop.py (17), test_experiments_api.py (4),
  2 Firestore parity tests (28/28 against a LIVE emulator). Full suite:
  **709 passed, 0 failed, 44 skipped**.

## v15.2.0 — 2026-11-01

### Phase 5 effect ledger on Firestore — the last durable-store parity gap

- **FirestoreEffectRepository** (`firestore/effect_repository.py`): the
  full §5.4/§5.5/§5.9/§5.11 contract — prepare-or-retrieve via a
  deterministic DIGEST-ANCHOR document (a query cannot lock an absence,
  so `effect_digest_index/{sha256(owner|operation|digest)}` created with
  transaction.create() carries the uniqueness: concurrent duplicates
  contend on that one document, exactly one wins), CANCELLED_BEFORE_
  DISPATCH frees the digest (partial-index parity), §5.11 version-CAS
  transitions, §5.9 receipts content-addressed by response digest (INSERT
  OR IGNORE parity), unresolved/run queries.
- **PROVEN against a LIVE emulator** (26/26 parity tests): transport
  retry preserves identity; CONCURRENT duplicate prepares (different
  effect ids, same digest) resolve to the SAME effect with exactly one
  created=True; CAS conflicts; receipt dedup; cancelled-digest reuse;
  plus the earlier browser/step/episode/admission/budget matrices.
- **End-to-end Firestore proof**: with STORAGE_BACKEND=firestore against
  the emulator, the full production path now works — effect prepared →
  DISPATCHING → OUTCOME_UNKNOWN through the state machine, execution
  step recorded, the evaluation adapter scores the run honestly (success
  False, tool_calls 1, uncertain_effects bound), and the record is
  readable from a FRESH repository instance.
- This closes the Firestore deployability gap for every durable store in
  Phases 3–12; previously the effect boundary raised "Effect repository
  not configured" on any Firestore deployment.
- Tests: 5 new effect parity tests in test_phase8_11_firestore_parity.py.
  Full suite: **688 passed, 0 failed, 42 skipped** (26 environment-gated
  + 16 pre-existing gates).

## v15.1.0 — 2026-11-01

### Firestore parity for the Phase 8/10/11 durable stores (§3.21 completion)

- **Execution steps on Firestore** (`firestore/execution_step_repository.py`):
  the same §10.5/§10.6 contract — deterministic `execution_steps/{step_id}`
  documents, CAS transitions inside async transactions enforcing the SHARED
  state-machine tables (both backends cannot drift), client-side sequence
  sorting (no composite-index prerequisite).
- **Recovery episodes on Firestore**
  (`firestore/recovery_episode_repository.py`): §11.7 atomic ownership
  claims (live-lease CAS + generation advance in ONE transaction),
  §11.3 CAS transitions, §11.8 abandoned sweeps (per-state equality
  queries — no composite index), §11.9 atomic field increments.
- **Browser sessions on Firestore**
  (`firestore/browser_session_repository.py`): §8.7/§9.0A transactional
  ownership acquisition (TENANT identity + lease + server-controlled
  generation resolve atomically), strict renewal (expired leases cannot
  be revived), CAS lifecycle transitions and controller updates, §8.10
  timer sweeps that never extend absolute lifetime.
- **PROVEN against a LIVE emulator**: started the real Firestore emulator
  and ran all parity suites — 21/21 passed (4 Phase-3 admission + 10 new
  Phase-8/10/11 + 7 budget). Two latent defects surfaced and were fixed:
  the capacity parity test itself had never run (missing release before
  the idempotency section; wrong AcquireOutcome attribute usage), and the
  Firestore duplicate-execution-key decision diverged from SQLite (it
  required BOTH worker ids non-empty; the SQLite reference contract
  compares `(worker_id or "")` — aligned).
- Factory `_init_firestore` now wires browser sessions, execution steps,
  recovery episodes, and evaluation records — closing the Phase 8–12
  Firestore parity gap.
- Tests: test_phase8_11_firestore_parity.py (10, emulator-gated). Full
  suite: **688 passed, 0 failed, 37 skipped** (21 environment-gated + 10
  Firestore-emulator parity + 6 pre-existing gates).

## v15.0.0 — 2026-11-01

### Phase 12 — evaluation data & outcome verification (§12)

- **Evidence-contract scorer**: `evaluation_runner.py` produces
  TRUSTWORTHY records from the Phase 10 evidence contract — acceptance
  criteria verified with the OutcomeVerifier (no criteria ⇒ needs_review,
  never a silent pass), effect-ledger states (uncertain/failed effects
  honestly fail the run), durable execution steps for tool-call counts
  and error-code accuracy rates, observed latency, and the real per-run
  token spend drained exactly once.
- **Durable records**: `evaluation_records` table + repository (upsert on
  evaluation_id), Firestore parity repository, wired into the factory
  for both backends. Records survive restart.
- **Fixture binding & scorer pinning**: every record carries
  `fixture_hash` (SHA-256 over task ids + criteria) and
  `evaluator_version` (2.0.0) — fixed fixtures reproduce identical
  results; scorer changes write new records, they never rewrite
  historical scores in place.
- **Worker run-finish path**: `_evaluate_terminal_run` now awaits
  `evaluate_run_async`, which gathers REAL effect states
  (`effects_for_run`) and execution steps (`steps_for_run`) from the
  durable repos before scoring. The legacy sync facade fails loud inside
  a running loop (a double compute would double-drain the token counter
  and lose the real spend).
- **API**: `GET /evaluations/runs/{run_id}`,
  `GET /evaluations/{evaluation_id}` (tenant-scoped),
  `GET /evaluations/summary` (admin-only aggregate: success rate,
  latency, tokens, recoveries). Missing repos fail closed with 503.
- **Frontend**: typed helpers in `api.ts`; `EvaluationPanel` mounted on
  the run workspace showing the verified verdict, per-task verifier
  verdicts, effect uncertainty, recovery/tool metrics, and the scorer
  version + fixture hash.
- Tests: test_evaluation_phase12.py (9), test_evaluation_evidence_
  integration.py (3), test_evaluations_api.py (4). Full suite:
  **688 passed, 0 failed, 27 skipped**.

## v14.0.0 — 2026-11-01

### Phase 11 — advanced self-healing: durable recovery episodes (§11.0–11.21)

- **§11.0 prerequisite closure**: acceptance criteria survive persistence
  and reload (Finding 3); effect-state verification uses the CORRECT
  EffectState enum values (Finding 2); engine completion consults the
  OutcomeVerifier (Finding 1); recovery deadline is active (Finding 7).
- **§11.2/§11.3**: durable recovery_episodes table with the full §11.2
  field set; a 12-state recovery state machine with per-actor CAS
  transitions.
- **§11.7**: atomic recovery ownership claims (CAS in BEGIN IMMEDIATE);
  concurrent workers → 1 winner; ownership generation advances.
- **§11.8**: abandoned episodes detected by lease expiry; another worker
  claims and resumes.
- **§11.9**: persistent attempt/model/tool/verification counters.
- **§11.0 Findings 8–11**: strict action validation (unsupported actions
  rejected), cycle detection using actual historical fingerprints,
  premature TASK_RECOVERED → TASK_REPAIR_QUEUED, escalation notification
  uses self.workflow_engine.
- Tests: test_recovery_episodes.py (9), test_recovery_worker_acceptance.py
  (5). Full suite: **672 passed, 0 failed, 27 skipped**.


## v13.0.0 — 2026-11-01

### Phase 10 — verified ReAct orchestration (§10.1–10.6)

- **Structured step records (§10.1)**: execution_step.py — observation,
  action proposal, policy decision, execution, and verification records
  with IDs, timestamps, evidence references, and content digests.
- **Untrusted content separation (§10.2)**: the observation is DATA; the
  policy decision is independent of page text; observation summaries
  truncated for display.
- **Bounded loop (§10.3)**: ReActBounds enforces iterations, wall time,
  spend, tool calls, and repeated equivalent actions OUTSIDE the model.
- **Outcome-gated completion (§10.4)**: model-declared completion requests
  verification but cannot alone establish external success — the verifier's
  verdict decides; pending uncertain effects block completion until
  reconciled; bounds exceeded block completion.
- Tests: test_react_execution.py (15). Full suite: **642 passed, 0 failed,
  27 skipped**.

### §10.0 review-closure additions

- **§10.1**: workflow acceptance dedup returns the original run (the C5
  dedup-continues-planning defect fixed); a canonical request digest
  detects same-key-different-payload conflicts; the Firestore initial-work
  contract is explicit (C2: SQLite callbacks into Firestore are refused
  with a typed contract error, not silently dropped).
- **§10.2**: the browser WORKER_ID import is fixed (the invalid
  `isolated_runner.WORKER_ID` import prevented browser startup on SQLite);
  cleanup marks complete only after the runtime sweep has run.
- **§10.8**: the permissive verifier is fixed — no criteria = needs_review
  (not pass); artifact_exists requires a RESOLVER (unresolvable =
  needs_review; nonexistent = fail); a generic click with only interaction
  evidence = OUTCOME_UNCERTAIN; credential-binding changes invalidate
  browser staleness context.
- **§10.5/§10.6**: execution_steps table with the §10.5 field set; the
  §10.6 state machine with 13 states and atomic CAS transitions; step
  evidence appended inside the transition transaction; the cancellation/
  completion race produces one winner + one conflict.


## v12.0.0 — 2026-11-01

### Phase 8 carryover closure + Phase 9 DOM targeting & verified actions (§9.0–9.22)

- **§9.0A Phase 8 defects closed**: tenant identity enforced IN the
  ownership-acquisition transaction; expired leases cannot be revived by
  renewal (controlled reacquisition required, which advances the
  generation); server-controlled monotonically advancing fencing
  generation (a caller cannot reuse an epoch by repeating an attempt);
  lifecycle transitions and controller updates use atomic compare-and-set;
  takeover grants die at their exact expiration boundary and compare
  tenant + epoch; the durable browser-session repository is wired into the
  factory, web_agent (allocate-before-launch, close-after-cleanup), and
  the worker sweep.
- **§9.0D stable business-action identity**: material digests exclude the
  ownership epoch — recovery keeps the SAME effect; a different payload
  gets a NEW effect; browser dispatch intent is recorded durably BEFORE
  the action executes.
- **§9.2/§9.3**: the structured observation contract (semantic roles,
  accessible names, containers, business identities, locator candidates,
  bounded scoped observation with explicit truncation) and the typed
  action union with per-action argument schemas (no JS/selector injection).
- **§9.4–9.8**: semantic target resolution with the documented order and
  business-identity scoping (ambiguity reported, never .first); staleness
  rules per change dimension with structured failures; the action pipeline
  validator (ownership, policy, approval digest, effect recording); outcome
  verification separating interaction/acknowledgment/business evidence
  (writes without business evidence → OUTCOME_UNCERTAIN).
- **§9.17**: progress tracking using meaningful signals (milestones, state
  changes) with bounded detection of repeated actions, ambiguity, empty
  observations, and uncertain-submission retries.
- **Concurrency (§9.21)**: four-worker concurrent races → 1 winner; strict
  grant expiry at the exact boundary; foreign-tenant acquisition denied.
- Tests: test_dom_targeting.py (23), test_browser_session_concurrency.py (4).
  Full suite: **620 passed, 0 failed, 27 skipped**. Real-browser fixture
  journeys (§9.20) require the local browser fixture harness and are the
  documented next step.


## v11.0.0 — 2026-11-01

### Phase 8 — browser ownership, isolation, recovery & handoff (§8.1–8.25)

- **Immediate fixes (§8.4)**: awaited page-state capture; done decisions
  recorded separately from executed steps (completion verification over
  executed steps only — observational tasks can complete without clicks);
  every browser entry point requires execution context (the orchestrator
  refuses browsing without run_id/task_id).
- **Durable session model (§8.5/§8.6)**: browser_sessions table with
  lifecycle, controller, challenge, and cleanup as SEPARATE dimensions +
  a permitted-transition matrix validating the actor; durable metadata
  separated from process-local Playwright handles.
- **Ownership leases + epoch fencing (§8.7)**: CAS acquisition (racing
  workers → 1 winner); epoch fencing (old worker's renewal fails after
  takeover); duplicate resume resolves to the same session.
- **Separate timers (§8.10)**: idle/handoff/absolute lifetimes; heartbeats
  never extend the absolute lifetime.
- **Network policy (§8.11)**: navigation vs resource vs websocket
  categories with normalized origins (alternate ports are distinct).
- **Credential injection (§8.12)**: origin/frame/purpose/controller/
  challenge/observation checks; challenge fields human-only; page-
  instructed wrong-purpose injection DENIED.
- **Action intents (§8.13)**: material digests bind approval to
  target+payload+session+epoch; non-form writes classified consequential.
- **Effect integration (§8.14)**: browser effects recorded before
  dispatch; restart with OUTCOME_UNKNOWN requires reconciliation — no
  silent repeat.
- **Takeover grants (§8.16) + recovery levels (§8.19)**: short-lived,
  epoch-bound, view≠control, revocable grants; honest recovery levels.
- Tests: test_browser_phase8.py (17). Full suite: **593 passed, 0 failed,
  27 skipped**. The same-session remote-control gateway (§8.15) and full
  supervision table are documented as the deployment-side next step.


## v10.0.0 — 2026-11-01

### Phase 7 — standalone MCP servers & isolated execution contract (§7.1–7.25)

- **Artifact model (§7.7)**: complete immutable package identity (source
  bundle + schemas + dependency lock + entrypoint + transport) with
  evidence/flags EXCLUDED from identity; unsafe source paths rejected;
  independent capability fields (export/build/protocol/isolation/deployment)
  — `deployable` is not the authority (§7.2).
- **Trusted attestations (§7.19)**: HMAC-signed by the control-plane issuer;
  bound to exact artifact+runtime+suite digests; forged documents and
  cross-artifact evidence rejected (tested).
- **Deployment service (§7.20)**: eligibility from current records — content
  change after testing blocks, protocol passes cannot bypass isolation,
  op-level Phase 6 eligibility enforced, revocation and caller checks (all
  tested).
- **Runner contract + profiles (§7.11/§7.12)**:
  submit/get/cancel/terminate/collect/cleanup with supervisor-enforced
  deadlines; the write profile is gated on the verified Phase 5 effect
  boundary; the default runner returns the explicit unavailable result
  until a backend is proven against the isolation profile (§7.5) — managed
  execution is NOT claimed as shipped.
- **Candidate diagnostics (§7.10)**: structured findings (syntax, imports,
  forbidden capabilities, embedded credentials, dynamic installs); a clean
  result is diagnostic-only.
- **MCP client (§7.17)**: call_tool preserves the COMPLETE result —
  content, structured output, and the tool's error status — so provider
  tool errors can no longer appear as successes; tool listing follows full
  pagination.
- **Completion pass (§7.5/§7.8/§7.9/§7.14-15/§7.16/§7.22/§7.24)**: the
  isolation acceptance profile (10 executable tests) + runtime decision
  record (Cloud Run sandbox selected_pending_proof, gVisor fallback); the
  unified artifact service (packaging + portable ZIP from the same bytes,
  deep-copied at the boundary); the dependency lock with integrity
  verification for all packages; the operation broker (capability-bound
  provider access — replay across execution/tenant/artifact rejected);
  the protocol validation suite classification (skipped-required ≠ passed);
  self-repair through immutable candidates (bounded proposer); kill
  switches + orphan reconciliation + telemetry (§7.24); ADR-006 updated
  with the real decision; runbooks for runner failure and artifact
  revocation.
- Tests: test_standalone_phase7.py (14) + test_runner_security.py (17).
  Full suite: **576 passed, 0 failed, 27 skipped**. Documented pending:
  runner backend live proof, live dependency build, adversarial isolation
  matrix execution.


## v9.0.0 — 2026-11-01

### Phase 6 — connector validation, provenance, revision activation (§6.1–6.23)

- **Provenance (§6.4)**: missing/unverifiable provenance defaults to
  `unknown` (never official_spec); provenance is preserved through the test
  route (the strip-and-reprobe no longer loses it); import method is no
  longer equated with publisher identity.
- **Canonical revision identity (§6.5)**: operations sorted by stable
  identity, keys sorted, mutable evidence and health excluded, digest
  format versioned — tool order and probe metadata can no longer change a
  revision id; definition changes do. Restores strip copied execution
  evidence.
- **Credential bindings (§6.7)**: evidence binds to the authorization
  CONTEXT (ref+version+epoch) via credential_binding_id_from — policy,
  coverage, factory, and routes all use it; no-credential connectors get a
  deterministic binding so attaching a credential invalidates old evidence.
- **Validation runs (§6.8)**: durable connector_validation_runs with
  START-context capture and atomic completion comparison — credential or
  definition rotation mid-run SUPERSEDES the run; superseded evidence
  cannot publish.
- **Coverage bypass closed (§6.1/§6.12)**: context-free coverage is
  historical and never claims current eligibility; current requires the
  spec digest AND the binding.
- **Eligibility evaluator (§6.12)**: one authoritative evaluator
  (connector_eligibility.py) with structured reason codes; enforced in
  tool_policy at dispatch and surfaced per-operation in the integrations
  detail API/page (§6.17).
- **Atomic activation (§6.14)**: CAS on the active pointer inside
  BEGIN IMMEDIATE + activation event; racing activations produce 1 winner
  + 1 explicit conflict; rollback requires CURRENT eligibility (§6.15).
- **Conservative migration (§6.22)**: idempotent; missing provenance to
  unknown; legacy probe results preserved as history; legacy verified
  labels NOT converted to per-op evidence.
- Tests: test_connector_phase6.py (22). Full suite: 545 passed, 0 failed,
  27 skipped. Controlled live-write validation (§6.11) stays gated until
  validation writes flow through the Phase 5 effect path (§6.2).


## v8.0.0 — 2026-10-31

### Phase 4→5 integration closure + Phase 5 external effects (§5.0–5.23)

- **C1–C16 closed (§5.0 Batches A–E)**: outbox envelopes route to DAG
  evaluation (C1); the Firestore acceptance interface is explicit (C2);
  queue accounting releases when work starts or terminates (C3); execution
  transitions sync the command lifecycle (C4); workflow AND schedule
  creation accept commands through the shared contract with receipts
  returned to clients (C5); command cancellation cancels the associated run
  and completion is CONDITIONAL — a run cancelled between read and write
  cannot be overwritten (C6); the build-waiter's undefined variable is
  fixed (C7); parent waits are created inside the atomic build acceptance
  and their resolution actually wakes the parent (C8); deferrals persist on
  the column the reconciler queries (C9); the reconciler receives the
  schedule repository for claim recovery (C11); command event history is
  backend-neutral (C12); the shared platform credential is ONE quota scope
  (C14); the workspace mounts the real command lifecycle (C15); submissions
  carry a stable Idempotency-Key reused across retries with the receipt
  returned (C16).
- **Phase 5 core (§5.2–5.12, §5.19–5.20)**: the effect ledger
  (effects/effect_receipts) with versioned canonical identity; the
  effect-execution boundary with structured outcomes (unknown is not
  failure); EMAIL as the first integrated path (per-recipient
  acceptance/refusal, stable Message-ID, partial outcomes, disconnect to
  OUTCOME_UNKNOWN with NO automatic resend); evidence-based reconciliation
  (absence is not proof) with conditional, audited resolutions; /effects
  API + UncertainOutcomePanel mounted in the workspace with a separate
  "Check outcome" control.
- Tests: test_effect_dispatch.py (10, through the service boundary with
  deterministic SMTP simulators). Full suite: 523 passed, 0 failed,
  27 skipped.
- Documented next integrations (§5.22 rollout order): MCP tool mutations,
  browser submission effects, batch/child effects, compensation execution.


## v6.5.0 — 2026-10-31

### Phase 3 — admission & capacity (§3.0–3.23)

- **Window/cleanup defect fixed (§3.4)**: the shared limiter stored a window
  ORDINAL while the worker purged with an EPOCH cutoff, so cleanup deleted
  ACTIVE windows and reset exhausted allowances mid-window. One epoch-second
  representation everywhere (`window_start/window_end/cleanup_after`), the
  interval is part of the key, cleanup is batched (500/loop), the cutoff is
  clamped to now, and migration recreates the old ordinal table.
- **First-record admission fixed (§3.5)**: zero allowance is refused BEFORE a
  counter is created in both SQLite and Firestore; negative/invalid policy
  inputs raise; empty identities are `POLICY_INVALID`, never unlimited.
- **Typed admission decisions (§3.8)**: `models/admission.py` replaces the
  ambiguous True/False/None helper — reason codes, enforcement modes,
  remaining/retry/reset, decision_id; HTTP maps healthy-quota denial to 429
  and store-outage denial to 503; the structured body is the frontend contract.
- **Policy inventory (§3.3)**: `security/admission_policy.py` is the one
  authoritative mapping (13 canonical groups + legacy aliases that share the
  canonical counter). Unknown groups raise a configuration error instead of
  silently inheriting the general allowance.
- **Per-workload outage modes (§3.9)**: workflow/MCP-build/browser-start fail
  CLOSED (503) on store outage; auth/contact/webhook/resume/studio fail OPEN
  to the bounded per-process limiter, labeled degraded and counted in metrics.
- **Shared enforcement wired (§3.10)**: /intent, workflow creation, all MCP
  build routes (funnel), webhook ingress (IP + owner), run-now, resume,
  studio generation.
- **Capacity leases (§3.11)**: durable per-tenant active caps for
  task/build/browser/provider/artifact classes — atomic acquisition, duplicate
  events re-acquire one lease, idempotent release, ownership-verified renewal,
  generation fencing against stale workers, per-process semaphore, TTL +
  ticker reconciliation. Engine acquires capacity before claiming a task,
  defers with bounded exponential backoff when a tenant is at cap, and
  releases in `finally` (success/failure/timeout/cancel/approval-wait).
  Browsers: new Chromium acquires; paused HITL sessions keep the lease;
  resumed sessions release by execution key.
- **Durable command admission (§3.12)**: `pending_commands` is the strict
  accepted-work counter with per-user/org/class caps and max-wait expiry;
  duplicate submissions return the existing admission; MCP builds admit at
  the `_start_build` funnel and never strand a slot on persistence failure.
- **Bounded local bus (§3.13)**: topic queues bounded with timeout-then-
  reject publishing; retries park in a bounded delayed-retry store moved by a
  pump that never blocks (the old sleep-inside-consumer requeue deadlocked a
  full queue); dead letters bounded (oldest dropped); pump cancelled on
  shutdown. Local mode is explicitly non-durable.
- **Pub/Sub flow control (§3.14)**: explicit max outstanding messages/bytes
  aligned with worker capacity; callbacks settle before the next delivery.
- **Atomic schedule admission (§3.15)**: `schedule_occurrences` claims
  `schedule_id + occurrence` atomically — duplicate deliveries resolve to one
  logical execution; a crash between claim and run-creation releases the
  claim for redelivery.
- **Fairness & provider protection (§3.16)**: per-tenant active/pending caps,
  per-class shares, provider-throughput admission per credential scope inside
  the metered attempt (before the Phase 2 hold), bounded jittered backoff
  honoring Retry-After.
- **Phase 2 coordination (§3.17)**: throughput admission → budget reservation
  → execute → settle → release; a budget refusal never strands a capacity
  slot; a capacity refusal never starts paid work.
- **Frontend overload states (§3.19)**: `AdmissionNotice` (six precise states,
  real Retry-After countdown, preserved draft) and `QueueStatusPanel` (accepted
  work only, no invented positions); `api.ts` gains `AdmissionErrorBody`,
  detection helpers, and jittered SSE reconnects.
- **Metrics (§3.20)**: `GET /admin/admission/metrics` — admissions/denials,
  store errors, local-fallback activations, capacity denials, lease expiries,
  command outcomes, and the live policy inventory; labels never carry user or
  command ids.
- **Ingress (§3.18)**: the `cloud_run` module exposes an explicit `ingress`
  variable so a load-balancer-fronted Cloud Armor policy cannot be bypassed
  via the default `*.run.app` URL.
- **Admin policy operations (§3.20)**: audited `POST /admin/admission/policies/{group}/disable|enable` — an explicit disabled state (not a magic number), reversible, with `ADMISSION_POLICY_DISABLED/ENABLED` audit records; there is deliberately no unaudited "reset all limits" endpoint.
- **Load check (§3.22-16)**: `scripts/loadtest_admission.py` — 8 OS processes x 50 attempts against one store: exactly the allowance (25) admitted and exactly the cap (15) leases created under contention; results recorded in RELEASE_EVIDENCE.
- **Every §3.10 entry point gated (completion closure)**: build admission moved into the connector-build SERVICE funnel so agent-requested builds (orchestrator `build_integration` tool) enforce the SAME shared allowance + command admission as API builds — denials surface to the model as non-retriable refusals (§3.16: subagents cannot bypass user limits). `core.mcp_build` tasks hold an MCP_BUILD capacity lease (per-tenant build cap) alongside the task lease. The SSE endpoint enforces the `event_stream` policy. Webhook deliveries are owner COMMANDS: pending caps apply, replays 409 via the ledger, and settled commands free their idempotency key. Command-admission caps now count pending AND dispatched work (a dispatched build still holds its slot), with max-wait expiry covering both.
- **Metrics depth (§3.20)**: shared-store admit latency, active lease units by resource class, per-class pending depth with oldest wait, and provider retry volume — all on `/admin/admission/metrics`.
- **Firestore parity (§3.21)**: `test_phase3_firestore_parity.py` — the shared-limit, capacity-lease, and command-admission contract matrix against the emulator (skips without one).
- **Frontend (§3.19)**: AdmissionNotice wired into the integration-create page (input preserved) and the schedules page (run-now refusals with retry).
- **E2E (§3.21)**: `frontend/e2e/admission-and-queue.spec.ts` — rate-limit notice with preserved draft, 503 outage state, retry-from-notice success flow, and the workspace QueueStatusPanel backed by the real run id; QueueStatusPanel mounted on the workspace page.
- **Tests (§3.21)**: new suites `test_distributed_rate_limits.py` (incl.
  independent connections and real OS processes), `test_admission_policy.py`,
  `test_capacity_leases.py`, `test_queue_backpressure.py`,
  `test_schedule_admission.py`, `test_provider_backoff.py`; v5.7 shared-limit
  tests updated to the epoch/typed API; conftest resets repo singletons per
  test. Full suite: **493 passed, 0 failed, 21 skipped** (17 environment-gated + 4 Firestore-emulator-gated parity), plus 4 Playwright e2e admission tests, 2 webhook admission tests, and an 8-process load check.


## v6.4.0 — 2026-10-31

### Phase 1 final pass — checklist audit and gap closure (§1.0–1.18)

- **1.8 Ownership transfer (the last open Phase-1 item)**: dedicated
  owner-only route `POST /orgs/{org_id}/transfer-ownership`. The repository
  operation runs one owned-connection transaction — guarded owner swap
  (`WHERE owner_id = ?`, so concurrent transfers admit exactly one),
  target-must-be-a-current-member, promote/demote in the same transaction
  (the single-owner invariant holds at every observable instant), verified
  in-transaction, with `ORG_OWNERSHIP_TRANSFERRED` audited atomically. The
  outgoing owner becomes an admin. Firestore parity added.
- **1.7 hardening**: invitation acceptance now satisfies the explicit SQLite
  requirement — the whole read-validate-write sequence runs on an owned
  connection in `BEGIN IMMEDIATE`. Fixed the ordering defect where accepting
  while already a member still flipped the invitation to `accepted` (it now
  stays pending and revocable), and `ORG_INVITATION_ACCEPTED` commits inside
  the same transaction (no membership without its audit record).
- **1.12**: the worker's background-authorization gate was extracted into one
  shared function (`authorize_background_execution`) — tests call the real
  gate, not a drifted mirror. Organization-owned runs now recheck the owner's
  CURRENT membership before execution: a removed member's queued work (or a
  deleted org) is denied with an audited `org_membership_lost` reason.
  Personal runs are unaffected.
- **1.13**: on any 403/404 from an org-scoped call the frontend clears the
  selected organization, its members, and oversight results (no stale
  privileged state), shows an actionable message, and refreshes the list.
  `ApiError` now carries the HTTP status.
- **1.16**: migration validation tests — idempotent re-migration, legacy
  runs stay personal after their creator joins an org (no retroactive
  sharing), pending invitations survive with the verified-recipient gate
  intact, and ownership is never repaired by assignment.
- **Fixed a latent Firestore bug**: `sidecar.py` used `firestore.async_transactional`
  without importing `firestore` — any Firestore invitation acceptance would
  have raised `NameError`.

**Phase-1/2 closure verification (same day):** provisioned OpenJDK +
firebase-tools and ran the real Firestore emulator (cloud-firestore-emulator
v1.22.0). The contract runs caught and fixed two genuine Firestore bugs —
`resend_invitation` was missing from the sidecar entirely, and
`transfer_ownership` performed a read-after-write that Firestore transactions
reject — plus the missing `firestore` import noted above. Final state:
427 backend tests pass with the emulator active (0 skipped: 10 org-parity +
7 budget-parity tests execute against Firestore), frontend E2E 20/20
(including the 4 budget-controls journeys), type-check and unit tests green.

## v6.3.0 — 2026-10-31

### Phase 2 completed — budget enforcement & usage accounting (§2.1–2.21)

- **Identity-based reservation contract**: every paid provider attempt holds a
  durable, individually identified reservation (`budget_reservations`) with a
  unique request key — replay of the same logical call returns the SAME hold,
  key reuse with a different payload is a typed conflict. Task / call /
  attempt identities stay distinct (§2.5).
- **Atomic multi-scope admission**: all accounting mutations run on
  dedicated-connection `BEGIN IMMEDIATE` transactions (SQLite) / Firestore
  transactions — the shared connection is never used, so the reproduced
  first-insert cap bypass is structurally impossible and concurrent OS
  processes cannot independently spend the same allowance (§2.8, §2.0).
- **Idempotent settlement with receipt digests**: a repeated settlement counts
  once; conflicting receipts raise `SETTLEMENT_CONFLICT` and go through an
  append-only, correction-id-keyed adjustment path (§2.7, §2.19).
- **Explicit state machine**: reserved → dispatching → settled/uncertain →
  released with optimistic concurrency (`record_version`); a timeout,
  cancellation, or crash never silently releases allowance — stale holds are
  investigated by a worker reconciliation sweep (§2.6, §2.13, §2.20-11).
- **Budget service + usage normalizer**: one coordination point for policy,
  admission, dispatch intent, and settlement; provider usage normalized into
  a common receipt (missing fields stay unknown, cached/reasoning tokens not
  double-counted, estimates labeled). Usage is captured BEFORE parsing so an
  invalid-JSON response still settles (§2.10, §2.12).
- **Every paid path metered (§2.11)**: shared text/JSON generation (including
  repair and Groq/Grok fallbacks), ADK Groq/Hugging Face adapters (per model
  invocation), embeddings, JD parser and resume tailor (converted from direct
  SDK calls), web-agent decisions, grounded search, and vision.
- **Fail closed**: missing or unavailable accounting blocks paid execution
  (`ACCOUNTING_UNAVAILABLE`, HTTP 503) — never admits for free; a
  `BUDGET_ENFORCEMENT_MODE=observe` rollout mode exists for mixed-version
  deploys (§2.7, §2.18).
- **Authoritative API**: `GET /usage/budget` (snapshot with explicit
  unavailable state) and `POST /usage/budget/check` (admission probe with
  structured `BUDGET_EXHAUSTED`/available/required errors); settings gain
  platform-ceiling clamping and 0/negative semantics (§2.15, §2.3).
- **Frontend**: `BudgetUsagePanel` (settled consumption, active holds incl.
  the uncertain subset, available allowance, UTC→local reset time,
  measurement quality), `BudgetBlockedNotice` wired into run creation, and
  the fake "5h rolling window" spend figure removed — context estimates are
  visually separate from spending (§2.16).
- **Migration without double counting**: pre-Phase-2 daily aggregates are
  preserved via a `legacy_seed` mirror; the metered path writes consumption
  exactly once — the durable write path is reserved for unmetered legacy
  callers (§2.18).
- **Observability**: admission/rejection/overrun/uncertain/settlement-failure
  counters (no user identifiers in labels) exposed via
  `GET /admin/budget/metrics` plus operator-triggered
  `POST /admin/budget/reconcile` (§2.19).

391 backend tests pass (7 Firestore-parity tests skip without
`FIRESTORE_EMULATOR_HOST`); frontend type-check, 31 unit tests, and the
production build (webpack mode) pass. Spec:
[docs/budget-accounting.md](docs/budget-accounting.md).

## v6.2.0 — 2026-10-31

### Phase 1 completed in full — remaining items 1.2, 1.5 resend, 1.7 revalidation, 1.13 preview

- **1.2 Central resource-authorization service**: `resource_authorization.py`
  answers "can this current actor perform this action on this resource in
  its current state" — resolve_membership / require_membership /
  require_permission (named actions from the Phase-1.4 matrix, evaluated
  against the CURRENT role, never a claim) / require_resource_owner. The
  orgs routes delegate; non-members 404, insufficient role 403.
- **1.5 Resend policy defined and enforced**: resend ROTATES the token,
  extends expiry by the full TTL, and PRESERVES the originally authorized
  role — it can never become a hidden role-escalation route. Admin
  invitations can only be resent by the owner. The old token is dead
  after resend.
- **1.7 Acceptance revalidation**: acceptance revalidates that the
  organization still exists before granting membership; an invitation to
  a deleted org cannot grant access.
- **1.13 Invitation preview**: `POST /orgs/invitations/preview` lets the
  invitee see the org name and offered role BEFORE accepting — read-only,
  no status change, token remains valid for acceptance afterwards.

330 backend + 31 frontend + 16 e2e green.

## v6.1.0 — 2026-10-31

### Phase 1 executed end to end (1.1 → 1.18)

With the previous rounds covering 1.5–1.7, 1.9–1.10, and 1.15–1.16, this
round closed the remaining items and produced the roadmap's deliverable:

- **1.1 Authorization inventory delivered**: `docs/AUTHORIZATION_INVENTORY.md`
  tabulates 24 protected entry points × actor × resource × required
  permission × enforcement location × regression test — the alternative-
  path map the audit required.
- **1.4 Named permission matrix**: `security/permission_matrix.py` —
  named actions (organization.invite_member / invite_admin /
  manage_roles / remove_admin / oversight, credential.use / manage,
  artifact.download, workflow.create / read) mapped to minimum org
  roles; owner-only actions explicit; organization roles are distinct
  from platform roles (an org owner is NOT a platform admin).
- **1.3 Verified recipient identity**: `email_verified` on the user
  record (Google provider claims verify; local signup starts
  unverified), persisted via migration, and gated at invitation
  acceptance by `REQUIRE_VERIFIED_EMAIL_FOR_INVITES` — both directions
  regression-tested (unverified 403 → verify → accept 200; policy off
  preserves the local flow).
- **1.12 Authorization into background execution**: the worker re-loads
  the run owner's CURRENT account state before executing any queued
  task; a disabled owner's task is denied pre-execution with an
  attributable `BACKGROUND_AUTHORIZATION_DENIED` audit event and
  acknowledged (not requeued forever). Active owners execute unchanged.
- **1.11/1.14 verified present**: password change revokes ALL refresh
  tokens and audits; account disable/enable audit `USER_DISABLED` /
  `USER_ENABLED`; audit payloads carry no tokens or secrets.

**1.18 completion checklist** — verified across the suite: acceptance-
only membership; concurrency-safe create/accept/revoke (DB invariant +
independent-connection tests); one matrix on every endpoint incl. the
legacy alias; owner protections; personal resources private (org_id
slice, never inferred); query-scoped lists/downloads; cross-credential
revocation; background re-check; credential ownership; frontend accept
UI; attributable secret-free audits; SQLite contract tests green;
conservative migration (org_id NULL = personal, never inferred).

327 backend + 31 frontend + 16 e2e green.

## v6.0.0 — 2026-10-31

### Phase 1 tenancy: DB invariants, one invitation service, adversarial suites

The detailed Phase-1 roadmap's outstanding items, closed:

- **1.6 Uniqueness as a database invariant**: a unique partial index on
  `(org_id, email) WHERE status='pending'` backs the application check —
  concurrent duplicate invitations across independent connections leave
  exactly one pending row (proven with two DatabaseManager instances).
- **1.5 One shared invitation service**: `organization_invitation_service`
  is the single creation/acceptance path; the invitations route and the
  legacy members alias both delegate to it. Role validation, member/
  owner checks, email normalization, token generation (hash-only
  storage), and audit events live in exactly one place.
- **1.15 Adversarial tenant isolation** (`test_tenant_isolation.py`, 5
  tests): personal-run listing/direct-read denial, owner-scoped schedule
  listing, foreign artifact download denial with real path-contained
  files, owner-scoped credential reads, removed-member oversight loss.
- **1.15 Membership concurrency** (`test_membership_concurrency.py`, 3
  tests, independent connections): duplicate-invite race, accept-vs-
  revoke race (one consistent outcome), expiration not blocking new
  invitations.

315→323 backend tests green; 31 frontend + 16 e2e green.

## v5.9.0 — 2026-10-31

### §7 explicit states + connector clarity; §9 Outcome Verifier

- **Explicit task states (§7):** one authoritative `runStateStyle`
  mapping — Queued, Running, Waiting for approval, Waiting for
  credentials, Recovering, Outcome uncertain, Completed, Failed,
  Cancelled — rendered on the builds page with per-task colors; unknown
  states fall back safely.
- **Connector lifecycle clarity (§7):** every integration card shows
  separate badges for artifact type, tested, verified / needs-review,
  and active — the "generated ≠ authenticated ≠ tested ≠ active"
  distinction the audit required.
- **§9 Outcome Verifier:** a bounded, read-only service with typed
  acceptance criteria (contains / artifact_exists / http_ok /
  schema_keys / non_empty) returning pass / needs-review / fail with
  reasons. It never mutates the output, never executes tools, and never
  approves its own repair. 6 tests; 5 frontend tests for the state
  mappings.

315 backend + 31 frontend + 16 e2e green.

## v5.8.0 — 2026-10-31

### Shared rate limiting + browser outcome verification

**§1 closed — the last process-local High finding.** Abuse-sensitive
groups (auth, contact, webhook, mcp_build) now enforce ONE durable
fixed-window limit across replicas and restarts: a guarded-upsert
counter in the shared database (SQLite) / transactions (Firestore),
wired through the factory, purged opportunistically by the worker.
High-volume groups deliberately stay process-local — a database
round-trip per request is the wrong trade for volume the edge (Cloud
Armor) enforces better; that split is documented in the evidence packet.
Cross-replica and window-reset regressions included.

**§6 — browser outcome verification and stale-target detection.**
A model saying "done" no longer automatically establishes success: a run
whose every executed step errored is flipped to failure with the
verification evidence attached. And the observe-decide-act loop now
detects **repeated ineffective actions** — the same action fingerprint
twice with an unchanged page raises for recovery instead of spinning
until the step budget runs out.

309 backend + 26 frontend + 16 e2e green.

## v5.7.0 — 2026-10-31

### Self-healing loop formalized; rewards verify outcomes; evaluations are real

**§3 — the recovery loop is now a bounded, deterministic policy.**
`RecoveryPolicy` gates every LLM recovery: per-category ceilings, a 15-
minute deadline, and a repeated-action detector (the same recovery with
the same arguments is refused even inside the budget). The
RecoveryWorker consults it BEFORE running the recovery agent — refusals
escalate into a structured incident (classification, attempt ledger,
recovery hint, user options) delivered as a notification and a failed
task with `[recovery-escalated]` evidence. 10 tests.

**§5 stage 1 — rewards verify outcomes.** The adaptive model policy
recorded success whenever the model runner finished without an
exception. It now records success only for a VERIFIED usable payload
(non-empty reply or real tool result): a silent-but-empty response
records a FAILURE reward for whichever model ran. Security and
authorization remain hard constraints outside the reward.

**§4 — evaluations are real.** The model client tracks per-run token
spend; the worker generates an evaluation record once per terminal run
carrying that real spend (no more placeholder zeros), and the per-run
counters drain exactly once.

**Also:** production-readiness.md was verified already reconciled with
the release evidence packet (the parallel session updated it before this
turn); per-run usage accounting covers planning, recovery, and browser
decision paths via the shared llm-context binding.

302 backend + 26 frontend + 16 e2e green.

## v5.6.0 — 2026-10-31

### Second-review Phase A: every release blocker and High finding closed

The 31-Oct focused review of v5.5.0 found two new release blockers and
six High findings. All are closed with regressions — 292 backend +
26 frontend + 16 e2e green.

**Release blockers:**
- **Membership acceptance bypass removed.** The legacy members route now
  creates a pending invitation (202) — `org_repo.add_member` is provably
  never invoked by it; membership exists only on ACCEPTANCE.
- **Usage commits.** `add_usage` now commits; the durability test uses
  two separate connections (the old test shared one and masked the bug).
- **Atomic budget reservations.** Every paid model call runs
  reserve(estimated max) → run → settle(actual): a guarded upsert makes
  near-cap racers lose atomically on both SQLite and Firestore; the
  budget check counts settled actuals plus open holds.

**High:**
- Firestore invitation acceptance is one transaction (invite read +
  membership create + status flip); the invitation doc id IS the token
  hash; a deterministic pending marker blocks duplicate invites atomically.
- Standalone-server protocol results bind `content_hash` of the immutable
  executable content; editing server code after testing invalidates
  deployability. Dependency locks are exact versions.
- Coverage counting collapsed to ONE authoritative function, digest-
  verified against spec hash + credential when provided (the stale
  duplicate is gone).
- Orchestrator builds now dispatch through the queued lifecycle and park
  the parent task as a durable approval; the worker resolves the approval
  and re-dispatches the task when the build lands. A restart at any
  point loses nothing.
- Browser consequential actions (form submits, submit-control clicks)
  pass through the persisted, argument-bound approval gate.
- Browser resources close by reference even without a registry entry
  (the Chromium leak); paused sessions carry a 30-minute lease swept by
  the worker.

**Deliberately deferred (documented in RELEASE_EVIDENCE.md):** shared
rate limiting (Cloud Armor / shared store on real topology), the
generated-code isolated runner, cloud recovery drills, live OAuth and
accessibility journeys.

## v5.5.0 — 2026-10-31

### The defining lifecycle proven: the self-extending loop runs end to end

**Page-48 happy path, verified locally** (`test_self_extension_loop.py`):
a waiting run whose capability is missing → the orchestrator requests the
build through the durable service (origin + parent_run_id link the
waiter) → the adapter is generated from a real OpenAPI spec → the live
probe passes against a local capability server → the connector registers
**verified** with artifact_type=adapter, provenance, and per-operation
coverage → the original run resumes and completes against the newly
acquired capability → the build record still points back at the
completed run. The only hop needing a model is the gap detection
itself; every durable, policy, and resumption mechanic is proven.

**Supporting work:**
- Loopback SSRF handling: numeric-loopback allowance is now a TEST-ONLY
  flag (`OpenAPIParser._LOOPBACK_TESTING`, default False). Workflow
  core.http keeps strict validation — a user workflow can never reach
  the app's own localhost; "localhost" the NAME stays rejected (DNS
  rebinding); the loop test enables the seam only for its own lifetime.
- Invitation **accept** UI: the invitee surface (paste token → accept)
  on the orgs page completes the F01 full-stack flow.
- G08 deliverable validation: rendered resume HTML opens and contains
  the user's actual facts with no placeholder leakage; invented
  employers are flagged; the ATS score is labeled heuristic (3 tests).
- F16 build gates verified: backend CI runs the pytest suite inside the
  candidate image with `--network=none`; frontend CI runs
  lint(max-warnings=0) + unit + typecheck before the image build.

283 backend + 26 frontend + 16 e2e green.

## v5.4.0 — 2026-10-30

### Clean-image verification: the runtime-parity gate is closed

The audit's F05/F16 "clean production image" acceptance is now
demonstrated locally with Docker (colima):

- **API and worker images build from a clean environment** and run
  **Python 3.11.16** — the exact shipped runtime the audit flagged as
  unverified (local dev was 3.14).
- **Full suite inside the images**: all 279 backend tests pass inside
  BOTH the API and worker images. The in-image run caught a real
  parity defect: `test_ephemeral_vault_key_is_never_written_to_logs`
  assumed a development `APP_ENV`; on the production-configured image
  the vault correctly refuses ephemeral keys. The test now pins the
  environment it tests.
- **Declared dependencies verified in-image**: `google.cloud.pubsub_v1`
  and firestore import; `pip check` clean (F05 acceptance).
- **Startup smoke**: the API container starts with SQLite config and
  reaches `/health` 200 with JWT keys via the documented secret-mount
  path; worker housekeeping runs.
- **Two real deploy blockers caught and fixed**: (1) the working tree's
  `backend/` had 0700 permissions, which copied into the image and made
  every module unimportable at runtime — permissions normalized;
  (2) production mode without JWT keys failed with a raw
  PermissionError — it now raises an actionable operator message
  ("provide JWT_PRIVATE_KEY / JWT_PUBLIC_KEY via Secret Manager and
  restart"), matching the audit's clear-disabled-state standard.
- Remaining cloud-only items: Pub/Sub-wired startup, backup/rollback
  drills, load evidence (need the actual cloud project).

279 backend green (local and in-image) + 26 frontend + 16 e2e.

## v5.3.0 — 2026-10-30

### Audit: uncertain-outcome reconciliation + accepted invitations

**F07 — no blind replay of irreversible actions, completed.** The
idempotency ledger gained the two remaining mechanics: a worker
housekeeping **sweep** quarantines crash-orphaned `running` entries as
`uncertain` (never `failed` — the external outcome is unknown), and
`reconcile_uncertain` lets an operator or provider receipt resolve an
entry to completed (zero-cost replay safe) or failed (retry needs a new
key). Both SQLite and Firestore implement them; replay of uncertain
entries stays blocked with an explicit message. 6 tests
(`test_uncertain_reconciliation.py`), including "the guarded action must
not execute again".

**F01 — membership now requires acceptance.** Full invitation lifecycle:
POST /orgs/{id}/invitations issues a one-time token (only its hash is
stored; listed invitations never expose it), the invited user accepts via
POST /orgs/invitations/accept, and membership is created atomically with
the create-only invariant. Email-matched to the accepting account;
expiry, revocation, single-use, and one-pending-per-email (an admin
cannot silently upgrade a pending invite) are all enforced. Both SQLite
and Firestore sidecars implement the parity contract. The orgs UI now
issues one-time tokens instead of silently adding members. 5 tests
(`test_org_invitations.py`), e2e updated to the invitation flow
(anti-enumeration asserted: unknown emails still receive invites, no
user-existence leak).

279 backend + 26 frontend + 16 e2e green.

## v5.2.0 — 2026-10-30

### Audit closure: explicit artifact products + release evidence packet

**G03 — explicit artifact products.** The connector product boundary the
audit required is now concrete: `artifact_type` on every manifest, with
**adapter** (declarative mapping, the default and only auto-activated
product) distinct from **standalone_server** (an explicit export
package). Packaging produces an immutable, content-addressed artifact —
versioned source, dependency lock, per-tool schemas — that ALWAYS ships
`deployable: false`; `mark_deployable` refuses unless initialize,
tools/list AND tools/call protocol tests have all passed on the exact
artifact (hash-bound). Nothing in the build pipeline calls packaging
automatically. 5 tests (`test_standalone_packaging.py`).

**F16 — hermetic tests verified.** The three OpenAPI tests the audit saw
fail on petstore3.swagger.io DNS already run through a deterministic
`getaddrinfo` stub — 6/6 pass fully offline; the audit's flaky-network
class is retired.

**Release evidence packet.** `docs/RELEASE_EVIDENCE.md` now tracks every
audit evidence requirement with its honest status: verified (local)
with the exact regression file, or pending with the reason (deployment,
provider accounts, or independent review). Includes the full F01–F16 +
G01–G08 change ledger. The packet's claim is explicit: repository-level
remediation is complete and tested; the release verdict beyond local
evidence is unchanged until deployment-stage items are demonstrated.

268 backend + 26 frontend + 16 e2e green.

## v5.1.0 — 2026-10-30

### Audit stage 4+: connector evidence, provenance, and one durable build path

**G02 — per-operation test evidence.** Every generated operation is now
stamped with explicit validation evidence ("not run") at generation; a
passing probe upgrades exactly one operation, and a coverage summary
(total / read_probe_passed / untested / mutating_untested) is computed at
build time, logged, persisted on the manifest, and surfaced in catalog,
integration-detail responses. Untested operations cannot inherit the
verified badge — ToolPolicy denies them per operation (test: an
integration with ten operations and one passing probe reports exactly
that coverage).

**G06 — spec provenance is the trust ceiling.** Operations carry
`spec_provenance` (official_spec / extracted_docs / inferred_description)
stamped at generation and preserved through probes. The build's trust
tier keys on the SOURCE: url/spec/openapi can earn verified;
docs/prompt stay pending_review however good the probe looks. Manifest
fields `spec_provenance`, `test_coverage`, `capabilities_omitted` are
backward-compatible defaults on old records. Tests
(`test_connector_coverage.py`, 7).

**G07 — one durable build path.** New shared
`connector_build_service`: the API entry point delegates to it
(record + workflow task + dispatch, origin=api), and the orchestrator's
`build_integration` now creates the same durable record with
origin=orchestrator and the waiting parent run before running the
factory inline with the build_id — stage logs and outcomes land in the
same durable place and a restart mid-build leaves a recoverable trace.
`mcp_builds` gained origin / parent_run_id / wait_reason columns
(SQLite migration; Firestore stores the full payload). Tests
(`test_connector_build_service.py`, 3).

**G08 — resume provenance.** New deterministic grounding check
(`verify_resume_provenance`): every employer, institution and degree the
model produced must literally appear in the source text; anything else
is flagged `needs_review` in the build result. The ATS score now labels
itself: `score_kind: "heuristic"` with an explicit note that it does not
predict ATS acceptance or employment outcomes.

263 backend + 26 frontend + 16 e2e green; ESLint clean.

## v5.0.0 — 2026-10-30

### Audit remediation verified: F01–F16 + G01–G08 against the 30-Oct audit

The full-stack audit (F01–F16 baseline + G01–G08 connector observations)
was re-verified finding by finding against the current tree, with the
gaps closed and every first-batch acceptance criterion encoded as an
adversarial regression test. 253 backend + 26 frontend + 16 e2e green.

**Security (F01–F04) — closed and proven.** New adversarial suite
(`test_audit_security.py`, 8 tests): duplicate membership cannot mutate
roles at the router OR repository (atomic create-only insert in both
SQLite and Firestore); disabled accounts receive no tokens via password
or Google login; a logged-out/revoked token is refused; a denylist
outage fails CLOSED (503, never allow); demotion/disable takes effect
immediately because authorization re-reads current account state rather
than trusting JWT claims; organization oversight exposes only explicitly
organization-owned runs — personal runs stay private before and after
membership, and task payloads are sanitized. To make org-owned runs
possible, `WorkflowRun.org_id` was added (SQLite column migration +
model field; Firestore persists the payload).

**Cloud correctness (F05/F06/F10) — verified closed.** `google-cloud-
pubsub` is declared in requirements; Firestore `claim_task` reclaims
RUNNING tasks with expired leases (fenced by attempt); Firestore user
repository has transactional email-uniqueness registry, transactional
failed-login counters, `set_user_active`, and honors
`FIRESTORE_DATABASE_ID`.

**Shared controls (F08) — implemented.** New durable usage ledger
(`usage_ledger` table / `usage_ledger` collection, atomic increments in
both backends, wired through the factory). Every LLM path records spend
durably; the daily budget check reads the durable total so the cap
survives restarts and holds across API/worker processes. Ledger failures
are explicit (503), never a silent pass. (`test_usage_ledger.py`, 4
tests.)

**Verified already-closed in the working tree:** F09 (trusted-proxy
parsing, bounded state, group quotas), F11 (single-flight refresh with
Web-Lock cross-tab coordination + 204 handling), F12 (GIS script-src +
frame-src + build-time client ID + clean disabled state), F13
(blog/contact moved to durable backend content repo with typed bounds,
rate limits, roles), F14 (sandbox: python3 handling, redacted logs,
clear unsupported-runtime failure, host-execution prevention test), F15
(dead-letter topics + quarantine subscriptions + bounded retries), G01
(trust_tier lifecycle enforced at discovery AND execution, probe
evidence bound to spec-hash + credential), G05 (cancellable build
observation), F16 lint (0 errors / 0 warnings).

**Still open (staged, per the audit's own sequencing):** G02 full
per-operation authenticated test packs beyond the read probe; G03
standalone MCP server packaging/deployment path; G06 spec-provenance
labels; G07 unified durable build service; G08 resume provenance
labels; release-operations drills (backup restore, rollback, load) which
require the actual cloud deployment.

## v4.9.0 — 2026-10-28

### The loom heals itself; the ink gained a glint

**Self-healing loop, visible.** Each of the nine threads now runs a
deterministic lifecycle on a staggered 26-second clock: the thread
*snaps* (a gap opens mid-curve), a fault state dims it while an amber
marker blinks at the break, then two knit fronts of light converge from
both ends and the thread sews itself back together. With nine staggered
threads, recovery is always happening somewhere in the frame — the
product's recovery agent, made visible.

**Self-evolving loop, visible.** Each blueprint sheet periodically
redraws itself with a fresh drafting pattern, stamps
"SELF-DRAFTED · REV NN" in its corner, and flashes briefly as the new
revision lands — the workspace improving its own blueprints, on screen.

**Loom vitals.** The footer now carries a real-telemetry chip fed by the
live `/health` endpoint: uptime minutes, storage engine, and the
recovery-loop status — hidden entirely when the API is unreachable,
never faked.

**Ink flow v2.** The accent-word gradient gained a luminous glint band
that rides through it (reads as a shine passing through the ink, not a
hue change), the cycle slowed to 9s, and the drafting dots now pulse
gently on every page. Both effects respect prefers-reduced-motion.

- 205 backend + 17 unit + 16 e2e tests green.

## v4.8.0 — 2026-10-28

### The loom sews, the ink flows

**Threading made visible.** Each of the nine planet-to-sheet threads now
carries a travelling pulse of light (custom line shader, additive
blending) — the sewing current that shows the planet stitching the
blueprints. Needle heads glow and throb; a dashed stitch row trails the
weave needle on every sheet; the sheets gained a sewn hem (dashed stitch
border) in their texture. The planet bobs gently and precesses on its
axis, so limb highlights drift.

**Ink flow on every page.** The accent words — "It's *drawn.*" on the
home hero, the italic accent in every public-page SheetHeader, the
Sandtable wordmark on login/signup — now animate a slow (7s) gradient
drift through the blue family (accent → purple → steel). No pink, no hue
chaos; disabled under prefers-reduced-motion and the reduce-data switch.
The animation lives at the end of drafting.css so it wins the cascade;
inline em colours removed from Integrations/Blog so the effect reaches
them too.

- 205 backend + 17 unit + 16 e2e tests green.

## v4.7.0 — 2026-10-28

### The canvas is the interface: HUD panel removed

The Planetary Loom control panel ("A world taking shape" card) is gone
from the home page. The scene itself carries every interaction now:

- **Focus**: click the planet or a blueprint sheet on the canvas; Escape
  returns to overview (unchanged).
- **Zoom**: scroll-wheel / pinch on the canvas (unchanged).
- **Tempo**: the play/pause + 0.5×/1×/2× dock in the footer (unchanged).
- **Scene fallback**: the "Preparing your 3D world…" / "Retry 3D scene"
  note moved into the footer, so the WebGL-recovery path survives the
  removal.
- Landing unit tests rewritten for the HUD-free layout (17 unit, 205
  backend, 16 e2e green).

## v4.6.1 — 2026-10-28

### The stragglers join the drafting board

Every remaining frontend page now carries the home page's identity:

- **Integrations**: serif display heading with klein-blue italic accent
  over the connector workbench.
- **Privacy / Terms**: mono "SANDTABLE LEGAL" eyebrow + serif titles.
- **Login / Signup**: serif "Sandtable" wordmark (static duotone — the
  hue-shift is gone here too).
- **Get-started**: bracketed console frame around the workspace loader.
- 205 backend + 17 unit + 16 e2e tests green.

## v4.6.0 — 2026-10-28

### Public pages in the home page's language; a better planet

**Public pages now speak the drafting dialect.** New shared `SheetHeader`
component: bracketed console frame, glowing mono eyebrow with volume
index, serif display title with klein-blue italic accent, quiet deck and
rule. Applied to About, Features, Pricing, Contact, Docs, Community and
FAQs; Blog got the serif treatment in place. Section headings carry mono
labels (MISSION / VISION / FIG. 01…), matching the home page's eyebrow
system.

- **The colour-shifting headings are gone.** A second `.gradient-text`
  rule ran a 4s infinite hue-shift (blue→purple→pink); on mobile that
  read as text changing colour while scrolling. Headings are now a static
  ink→klein-blue duotone.
- **Reveals no longer look like colour shifts.** Scroll-reveal tightened
  from 1s/42px/blur-3 to 0.65s/22px/blur-2 — elements arrive crisply
  instead of lingering half-transparent.
- **Camera zoom hidden on mobile** (pinch already dollies the canvas);
  needed a specificity bump because the base rule lands later in the
  bundled cascade.
- **The planet got a proper surface**: half-lambert shading, fresnel
  cyan rim, faint latitude wash, finer graticule, drifting rings, and an
  additive atmosphere shell hugging the limb — verified in both themes.
- TiltCard now forwards arbitrary props (data-scroll-reveal works on
  pricing cards) and its hover shadow joined the klein-blue family.
- 205 backend + 17 unit + 16 e2e tests green.

## v4.5.0 — 2026-10-28

### NORTH LIGHT retold, pages polished, subagents verified live

**Light theme rebuilt around blue ink on drafting paper.** The old NORTH
LIGHT mixed olive-green text/borders with a blue paper and a klein-blue
accent — the hue clash read as muddy. Ink, borders, glass, status colors
and shadows are now one cool blue-gray family.

- **Fixed the fuzzy checkerboard on every public page**: the
  `.public-pages .mesh-gradient` override swapped the background image but
  kept the 44px tile size, tiling a single radial into soft squares.
  Public pages now share the crisp drafting grid with the home page.
- **Sketch illustrations are theme-aware**: the hand-drawn sketches were
  unconditionally `invert(1)`-ed (right on dark, a black box on light).
  `multiply`/`screen` blend modes now drop the paper background on both
  themes.
- **HUD slimmed** (desktop and mobile): tighter paddings, 20px title,
  34px view buttons, denser readings — the Planetary Loom control panel
  takes ~30% less vertical space.
- **More motion**: staggered scroll-reveals (`--reveal-delay`) wired into
  About (team cards, CTA), Features (text columns) and Pricing (new
  "Every plan includes" strip); old-identity leftovers fixed (pink card
  shadow, maroon badge text, Veyrune "V" footer marks → "S").
- **Backend: shared HTTP connection pool** (`backend/services/
  http_pool.py`) — core.http and the MCP tool executor built a fresh
  `httpx.AsyncClient` per call (new TCP+TLS handshake every time); both
  now reuse keepalive connections, closed on app shutdown. Per-call
  timeouts, SSRF validation and redirect rules unchanged.
- **Subagent health verified against the live backend**: core.health and
  core.http runs COMPLETED; MCPFactoryAgent built the 19-tool Swagger
  Petstore MCP from the live OpenAPI spec; worker processed every task;
  ResearchAgent and OrchestratorAgent construct cleanly (their planning
  runs need an LLM key, reported honestly by /health).
- 205 backend + 17 unit + 16 e2e tests green.

## v4.4.1 — 2026-10-27

### Dashboard sweep: two real bugs found and fixed
Swept all 14 `/dashboard/*` routes at 1280×800 and 390×844 with a live
backend. Two genuine defects surfaced; both fixed and verified.

- **CSP blocked the API when the URL differs from the default.**
  `connect-src` in `next.config.ts` was a second, hardcoded copy of the
  API URL (`localhost:8000` + `127.0.0.1:8000` only). Point
  `NEXT_PUBLIC_API_URL` anywhere else and the browser silently blocks
  every fetch with an opaque "Failed to fetch" while the server logs
  nothing. The CSP now derives its origins from `NEXT_PUBLIC_API_URL`
  itself (plus the localhost/127.0.0.1 sibling spelling), so it can never
  drift from the URL the client actually calls.
- **Workspace hero hid under the header on short viewports.**
  `.workspace-home` used `justify-content: center`; when its content grew
  past the pane (mobile height, or desktop with the MCP how-to panel), the
  overflow escaped *upward*, pushing "Where should we begin?" behind the
  sticky header where it could never be scrolled to. Replaced with the
  auto-margin centering pattern (centers when it fits, top-aligns when it
  overflows) and made the pane scroll its own overflow.
- Verified: full dashboard sweep clean at both viewports; 205 backend +
  17 unit + 16 e2e tests green.

## v4.4.0 — 2026-10-19

### Draughting Lamp labels restored
- The day/night toggle labels restored to **TUNGSTEN** / **NORTH LIGHT**
  (the Planetary Loom session had changed them to NIGHT/DAY).

## v4.3.0 — 2026-10-19

### Zoom that actually zooms
- **Range widened 0.4–2.8** (was 0.8–1.6): the camera now dollies from a
  wide establishing shot to a close-up on the planet — a 7× distance range.
- **Finer steps**: 0.05 instead of 0.1 — smooth, not chunky.
- **Full 3D dolly**: the zoom scales the entire eye offset vector (not
  just Z), so the camera moves properly in 3D space.
- **Scroll-wheel zoom** on the canvas: scroll up to zoom in, down to zoom
  out — the expected interaction for any 3D scene.
- **Pinch-to-zoom** on touch devices.
- Verified: the zoom slider + scroll-wheel both drive the camera to 280%
  with the planet filling the frame.

## v4.2.0 — 2026-10-19

### Regression analytics + perf + security
- **Run regression model** (backend/services/run_analytics.py): per-agent
  EWMA baselines (expected duration ± std) learned from real run history,
  with z-score anomaly detection for statistically slow runs. Surfaced on
  the Operations page ("Expected durations" + "Slow-run anomalies").
  4 unit tests (Welford math, conservative small-n, outlier flagging).
- **GZip compression** on all API responses over 1KB — real transfer-size
  win for JSON-heavy endpoints.
- **Refresh-endpoint rate limit** — /auth/refresh now sits behind the auth
  rate limiter (was the last unthrottled credential surface).
- **security.txt** (RFC 9116) served from /.well-known/security.txt.
- **Lamp chip collapses to icon-only** on phones; topbar title shrinks.

## v4.1.1 — 2026-10-19

### Fix: stuck-blur reveal bug + softer grid
- The v3.5 blur addition to the scroll-reveal entrance was never reset by
  `.is-in-view` — revealed cards stayed permanently blurred (visible on
  About/Features). The in-view state now resets the filter to blur(0).
- Dark drafting-grid backdrop softened (lower grid contrast).

## v4.1.0 — 2026-10-19

### Page transitions + full reveal coverage
- Root template.tsx: every navigation fades up into place (Apple curve).
- Reveals swept onto faq and docs/[slug] — all pages animate on scroll.

## v4.0.0 — 2026-10-19

### The Planetary Loom — the full cyanotype identity
The home page is now a single-viewport 3D experience built to the Stitch
spec: a dark drafting hall where a **planetary loom** knits blueprints.

- **The Planet**: a metallic graticule globe (standard material, torus
  rings, wireframe shell, atmosphere) at the loom's center, turning slowly.
- **Nine threads** in three strands weave from the planet to **three
  morphing blueprint sheets** — each sheet is a displaced-plane shader whose
  surface ripples as the weave progresses, with a wireframe model
  (icosahedron/box/octahedron) assembling in front of it piece by piece.
- **Interactive**: click the planet or the foreground sheet to focus
  (camera dollies in), Esc/reset to return; camera zoom slider (80–160%);
  tempo controls (0.5×/1.0×/2.0× + pause); pointer parallax.
- **HUD chrome**: Planetary Loom panel (focus buttons, zoom, THREADS 09 /
  BLUEPRINTS 03 MORPHING, FOREGROUND WEAVE meter), scene-controls tempo
  dock, module dock (BOARD · STAMPS · BUILDS · SHEETS), axis ruler,
  corner brackets, coordinate marks.
- **Graceful degradation**: scene fallback → still illustration → retry
  button; WebGL context-lost handling; reduced-motion support.
- **Day/night**: the Draughting Lamp (NIGHT ↔ DAY) in the navbar re-skins
  the loom live — North Light floods the hall with pale paper light.
- Verified at 1440×900 and 390×844; the loom visibly animates between
  frames (planet turns, weave meter advances 50%→63%).

## v3.11.0 — 2026-10-19

### Fresh deep-audit fixes (Round 38)
- **Budget hard-stop bug**: `check_budget` treated a 0 daily limit as
  missing (`0 or default` → 1M) — the enforcement never fired at 0. Fixed
  in both layers; verified live (429 at limit 0, 201 after restore).
- **Credential-error leak**: LLM-dependent goals without any API key
  returned HTTP 500 with the raw Google ADC error. Now classified to an
  honest 503 with guidance (both /workflows and /intent handlers,
  case-insensitive), and the generic 500 never leaks provider errors.
- **TokenStatusCard guards**: partial /usage payloads crashed the settings
  page (toLocaleString on undefined) — all fields guarded.

## v3.10.0 — 2026-10-19

### Composition + scrollbars + reveals finalized
- **Planet centered** in the frame; **sheets spread** (right sheet further
  right, left sheet toward center) so the hall fills the screen with clear
  gaps — no more merging.
- **No scrollbars on the home**: the wrapper is exactly 100svh with
  overflow hidden (verified scrollHeight == clientHeight).
- **Hero hover flipped**: readable by default (80%), hover turns it
  translucent (38%) so the hall shows through while the dark backdrop
  keeps the words readable.
- **Docs card art toned down**: muted slate-navy blueprint instead of the
  oversaturated klein blue.
- **Hero de-smeared**: solid dark console (86% + light blur) instead of
  the frosted panel that smeared the bright sheet behind it.
- **Scroll reveals on every page** (public + dashboard, +15 in this round;
  deeper translate + scale + blur entrance).
- Theme assertions in the e2e suite updated to the cyanotype palette —
  **16/16 e2e** passing.

## v3.9.0 — 2026-10-19

### Readability + smoothness + the theme button
- **Draughting Lamp in the navbar** (every page): TUNGSTEN ↔ NORTH LIGHT
  toggle next to the brand, dispatching a theme event the home scene
  listens to — the 3D hall re-skins instantly.
- **Hero hover-to-read**: the hero card is translucent (42%) by default so
  the hall shows through; hovering raises it to 88% opacity so the words
  are easy to read. Verified both states in the browser.
- **Smooth blueprint reprints**: reprints now CROSSFADE — two stacked
  meshes (previous pattern fading out, new pattern fading in with a cubic
  ease-out over 1.4s) instead of a hard texture swap. No more rough jumps.
- **Apple-style reveals**: the scroll-reveal curve is now
  cubic-bezier(.16,1,.3,1) over 1s — the smooth, decisive ease.

## v3.8.0 — 2026-10-19

### The scene IS the page
- **The planet is centered**: the graticule globe now sits in the middle
  of the composition with **four wireframe cubes orbiting** it.
- **More 3D objects**: a torus-knot and a dodecahedron join the hall's
  wireframe menagerie.
- **Blueprints reprint at random times**: every 6–13 seconds a sheet
  flashes and its drafting blocks change — plans being redrawn in front
  of you (pseudo-random per variant, visible brightness sweep).
- **Hero translucent**: 52% frosted panel with light blur — the hall
  shows through the card.
- **Scroll animations on every page**: reveals swept onto all 11
  dashboard pages and every public page, with a deeper entrance
  (translate + scale + blur).

## v3.7.0 — 2026-10-18

### The knitting hall, richer
- **Planet halo**: a blue atmosphere glow around the drafting globe — it
  reads as the lit center of the hall.
- **Multi-strand weaving**: six threads now run from the planet — three
  strands woven to the big stamped sheet (landing on separate cells), two
  to the left sheet, one to the back sheet.
- **Docs flow chips tinted** for the dark theme (blueprint glass instead
  of gray); docs [slug] pages verified matching the theme.

## v3.6.0 — 2026-10-18

### Responsive home + tempo control removed
- **Chrono-Pulse tempo control removed** from the home page (the hall
  runs at its own pace; the scene keeps the mechanism internally).
- **Fits every screen**: the home stacks on phones (hero above panels),
  the axis ticks hide on small screens, the navbar keeps Sign in only
  under 480px, and the dock/secondary links wrap. Verified at 390px,
  768px, and 1280px.

## v3.5.0 — 2026-10-18

### Palette discipline + planet separation + reveals everywhere
- **Docs cards toned down**: the klein-blue card art was oversaturated —
  re-painted to a muted slate-navy blueprint with softer ink (8 SVGs).
- **Hero de-smeared**: the frosted 34% panel + heavy blur smeared the
  bright sheet behind it into a blue blob. The hero is now a solid dark
  console panel (86% + light blur) — crisp text, and the scene stays
  visible around it.
- **Planet separated**: pulled left and back so the graticule globe no
  longer merges with the sheet's edge; threads arc across the clear gap.
- **Scroll reveals on every public page** (privacy, terms, docs/[slug],
  community, get-started added; +15 reveals) — all pages animate on
  scroll now.

## v3.4.0 — 2026-10-18

### The hall shows through
- **Hero card resized** (max-width 440, tighter padding/type, smaller CTAs
  and chips) and made **more transparent** (frosted 34% panel) — the
  planet, threads, and sheets are no longer hidden behind it.
- **Planet moved into the open gap** beside the big stamped sheet — the
  graticule globe now reads clearly as the object weaving the threads
  into the blueprint (thread arcs visible landing on the sheet).

## v3.3.0 — 2026-10-18

### Planet knitting + docs re-papered
- **The planet knits**: the planet moved into the visible gap (bigger,
  1.35× scale) and its threads are now **thick glowing tubes** that draw
  progressively, carried by a bright knitting head. Where each thread
  lands on a sheet, a **wireframe patch materializes cell by cell** —
  the blueprint being knitted into existence is visible.
- **Docs card art recolored**: all 8 docs SVG flowcharts re-painted from
  pink/cream to the cyanotype palette (blueprint base, light-blue boxes
  and arrows, light ink) — light/dark both read correctly.
- Docs cards gain bracketed console frames.

## v3.2.0 — 2026-10-18

### Fixes + the weaving node
- **Integrations nav link** now opens the public Integrations page
  (it previously bounced logged-out visitors to sign-in).
- **Thread weaving nodes**: a glow sprite rides the draw head of each
  planet-thread — the draughtsman stitching the blueprint onto the sheet
  is now visible, then hides while the thread holds.
- Get Started → workspace verified end-to-end in a real browser with the
  backend running (guest creation → /dashboard); note for local runs: the
  backend must allow the frontend origin (CORS_ALLOWED_ORIGINS).

## v3.1.0 — 2026-10-18

### The Planet threads the blueprints
- **The Planet**: a drafting globe (prussian body, blueprint graticule —
  meridians and parallels) turning slowly in the hall, weaving **threads**
  that arc to every sheet and draw stitch by stitch — the blueprints are
  threaded into existence by the planet.
- Home headline reduced so the hall reads; the hero card **floats gently**
  up and down (7s cycle, reduce-data aware).
- **Public nav restored**: About Us · Features · Integrations · Docs ·
  FAQs · Blog · Contact.
- **Docs page re-papered**: the shared backdrop is now cyanotype drafting
  paper (blue grid on near-black / pale paper) for every public page —
  docs, about, features, faqs, blog, contact, integrations all match.
- **Scroll reveals** across public pages (glass panels and sections fade
  up as you scroll).

## v3.0.0 — 2026-10-18

### The Drafting Works, faithful to the spec
- **No rotation**: the camera never turns — it holds the drafting angle and
  GLIDES laterally with a gentle bob. Sheets hold their drawn angles;
  structures barely drift. All motion comes from the hall itself: the
  planner's tracer light running its path, beams sweeping, dust drifting,
  back sheets unfolding.
- **Chrono-Pulse tempo control** (top-right): 0.5x / 1.0x / 2.0x / STEP —
  a real control that scales the hall's shared animation clock (STEP
  advances one manual frame).
- **Spec HUD chips**: WORKS · 7 CREWS · 23 STAMPS · 142 SHEETS TODAY and
  TOLERANCE · HELD alongside the real ENGINE chip.
- **Module nav**: Board / Stamps / Builds / Sheets + "Sign in →".

## v2.9.0 — 2026-10-18

### Moving background + restored nav
- **The background is now moving, continuously**: the camera orbits the
  drafting hall (slow continuous angle sweep) — sheets parallax against
  each other as the viewpoint changes, every frame different. Drag the
  hall to swing the orbit manually.
- **Sheet motion amplified**: stronger vertical drift, breathing rotation
  on Z and Y — the unfold reads clearly.
- **Public nav restored**: About Us · Features · Integrations · Docs ·
  FAQs · Blog · Contact (module dock stays at the bottom of the home).

## v2.8.0 — 2026-10-18

### Observatory chrome + ambient hall (zoom removed by design)
- **Click-to-zoom removed** — the scene is pure atmosphere now: pointer
  parallax (the hall drifts as you move), sheets that breathe and
  **unfold** (rotation oscillates open like a drawing being spread), three
  planner light streams tracing their paths, teal wireframes, beams, dust.
- **Axis ruler strip** under the nav (AXIS::X.DRAFT · tick marks ·
  CALIBRATED // ISO-216-A1) and **corner coordinate marks** — the
  reference's observatory chrome.
- Dock links get hover lift + glow; panels tightened; the whole home is
  one uncluttered viewport over the living hall.

## v2.7.0 — 2026-10-18

### Vivid drafting hall (matched to the reference)
- **Big saturated sheets**: blueprint planes reworked — deep blue base with a
  diagonal sheen, fine + heavy white grids, drafting blocks (X-marked
  rectangles, reticles, "COMMUNICATION LATTICE" / "FIELD MODULES" / "TOR
  ANTENNAE" labels), bottom title strip, and the **FOR APPROVAL stamp
  printed on the sheet** (rotated, validated line).
- Sheets repositioned large and tilted like the reference: foreground
  stamped sheet right, mid sheet left, back sheet top.
- **Diagonal light beams** from the top (additive shafts, breathing).
- **Teal icosahedron** + cone wireframes (the reference's mixed accents);
  tracer glow dot printed on each sheet.
- Low-angle parked camera (floor visible, sheets above the horizon).
- Click-to-zoom verified on the new sheets (readout + camera dolly).

## v2.6.0 — 2026-10-18

### The scene IS the navigation (interactive observatory)
- **Click-to-zoom**: clicking a blueprint sheet or wireframe structure dollies
  the camera in and raises a focus readout — "SHEET-002 // EXECUTION PLAN ·
  OPEN BUILDS →" — linking straight into the matching module. Click the hall
  or press Esc to pull back. Hover highlights targets with a lens cursor;
  the camera drifts with the pointer (observatory parallax).
- **Observatory chrome**: film-grain overlay (SVG turbulence), frosted glass
  panels, bracketed/chipped console frames (corner brackets in the accent
  blue), HUD chips in mono.
- **Palette**: light-blue accent (#8ab4ff dark / prussian light) with a
  single warm-orange accent reserved for the human-approval surfaces
  (FOR APPROVAL stamp, ENGINE lamp, APPROVALS chip).
- **Home tightened**: the scene shows through (pass-through UI layer — the
  fix that made click-to-zoom possible at all), panels concise, secondary
  page links (About/Features/Docs/FAQs/Contact) under the module dock.

## v2.5.0 — 2026-10-18

### SANDTABLE — "The Drafting Works" (Cyanotype redesign)
- **Renamed again, by design**: Sandtable (the Operations Room). A sandtable
  is where operations are rehearsed before they are real — exactly what the
  product does with your work.
- **Cyanotype theme**: near-black drafting hall `#04060B`, klein-blue accent
  `#3E6BFF`, fonts Fraunces (display) + Space Grotesk (body) + JetBrains Mono
  (telemetry). Day = "North Light" (pale drafting paper, prussian ink);
  night = "Tungsten". The lamp toggle in the nav.
- **Full-viewport WebGL scene** (Three.js / React Three Fiber, dynamic
  import): giant blueprint sheets hover and unfold; light streams trace the
  planner's path; wireframe structures assemble; drafting primitives stand
  in the hall; grid floor + graphite dust. No cosmos, no stars — pure
  drafting works.
- **Home page rebuilt clean**: one viewport — glass hero card ("Say it.
  It's drawn. It's built."), two honest panels (The Planner, Stamp Status
  // FOR APPROVAL), HUD chips with REAL engine state from /health, and the
  module dock: BOARD · STAMPS · BUILDS · SHEETS.
- Module naming across the app: Board (chat) · Stamps (integrations) ·
  Builds (runs) · Sheets (operations/metrics).

## v2.4.0 — 2026-10-18

### Organizations UI
- `/dashboard/orgs`: create orgs, add members by email, the role ladder
  inline (owner-immutable), remove members, and **run oversight** — a
  read-only view of member runs with scoped fields. Sidebar entry +
  2 e2e specs (14 e2e total). Visually verified in a real browser:
  the renamed brand, forge core, and both themes render correctly.


### Product renamed: Veyrune → **Sandtable**
- Sanskrit *yantra* — "machine/instrument" — the workspace that builds its
  own instruments. The historian tie: the 11th-century treatise
  *Samarāṅgaṇa-sūtradhāra* catalogs yantra automata. All user-facing
  surfaces renamed (117 replacements); repo/package identity stays AgentOS.

### 3D forge design system
- New design tokens: ember/steel identity, forge gradients, bevel + depth
  shadows; glass cards gain material thickness and a lit top bevel.
- `.panel-3d` perspective-tilt cards with specular sheen; metallic
  primary buttons with pressed depth.
- The **forge core**: a CSS-3D animated yantra (three counter-rotating
  rings around a molten core) in the home hero and via the new BrandMark
  (isometric yantra logo) in the sidebar, home footer, and auth.

### Theme: light / dark / system
- New resolver (`src/lib/theme.ts`): `system` follows the OS via
  prefers-color-scheme with live updates; first-paint script resolves
  before hydration; Settings gains the three-way selector.

### BYOK + token information
- New "Model & tokens" card on Settings: active provider + whose
  credential is in use (BYOK status), 5h rolling-window usage vs your
  daily budget with a progress bar and reset countdown.
- 3 new unit tests (theme resolver) — **11 unit tests** total.

## v2.2.0 — 2026-10-18

### Organizations (enterprise RBAC foundation)
- **Orgs + membership with a role ladder**: owner > admin > member > viewer.
  Create orgs, add members by email (anti-enumeration preserved — a missing
  email 404s without revealing account existence), change roles (owner only),
  remove members (owner-protected; admins manage members/viewers only).
- **Run oversight**: org admins/owners get a read-only view of member runs
  with scoped task fields (id, title, agent, status, error) — never full
  input/output payloads.
- Every membership change is audited (`ORG_*` events). Shared vaults and
  org-scoped workspaces remain the documented next steps.
- 9 new RBAC tests (201 backend total); full lifecycle verified live.

### v2.1.1
- Session-expiry regression tests (the deployed build's mid-action logout
  bug), guest-creation retry, model A/B ops guidance.

## v2.1.0 — 2026-10-17

Eight remediation-and-hardening rounds against the external audit
(`VEYRUNE_AUDIT_REPORT.md` rounds 3–10). 192 backend tests, 10 frontend
e2e tests, all three Docker images build and pass runtime smoke.

### P0 (audit-blocking)
- **Fixed invalid-JSON responses at the root**: lone UTF-16 surrogates in
  untrusted text (scraped HTML, PDF text, LLM output) crashed storage and
  corrupted responses. One canonical JSON layer (`SafeJSONResponse` on
  every response, safe SSE frames) plus surrogate repair at the input
  choke point. Property-tested with hostile payloads.
- **Server-side logout**: `token_denylist` store (SQLite + Firestore);
  a logged-out token 401s immediately instead of surviving its TTL.
- **Workflow rate limit** tightened to 10/min per user (quota-burn loophole).
- **OpenAPI relative server URLs** parse, resolve against the spec URL,
  and are SSRF-revalidated — petstore-style specs build again.
- **`GET /api/v1/health`** with version, baked commit SHA, uptime, and
  LLM credential status.

### Security & enterprise
- Per-user **API keys** (`vry_…`): hash-stored, shown once, individually
  revocable — stable auth for MCP clients and CI.
- **Daily token budget with a hard stop** on workflow creation, fed by
  usage recording at the LLM choke points (was dead code).
- **Outbound run webhooks**: HMAC-signed POSTs on run completion,
  SSRF-validated URL, Vault-configurable signing secret.
- **Data retention**: `RETENTION_DAYS` purge of events/audit rows (runs
  never auto-deleted).
- Rate limits on every state-changing surface; pagination guards
  everywhere; production CSP without `unsafe-inline` scripts.
- **Audit log**: API + admin Control Center UI.

### Product
- **Shared integration catalog**: publish verified integrations, browse,
  one-click per-user install (credentials never copied).
- **Self-evolving model routing**: UCB1 bandit re-ranks the model
  fallback chain from real task outcomes; leaderboard at
  `GET /api/v1/usage/models` and the Operations page.
- Task `title` + `started_at` populate end-to-end (timeline, DAG,
  details panel); details panel reports results as readable summaries.
- Schedules: run-now materializes the run synchronously (immediately
  observable), max-concurrency guard, skip-missed catch-up, missed-run
  notifications, `is_enabled` contract fixed.
- Chat replies report full health facts (status, latency, TLS expiry,
  grade, missing headers) instead of a bare status code.
- resume/scan: filename-safety, transient retry, honest error codes.

### Performance & ops
- `list_runs` N+1 eliminated: 6.0ms → 2.0ms p50 at 100 runs (3×).
- Memory writes under load: 5,835ms → 20.3ms p50 (embedding fail-fast
  with cooldown).
- Dockerfiles fixed for the unprivileged runtime user (file modes, JWT
  keys dir) — all three images build and pass runtime smoke.
- `scripts/gcp/deploy-safe.sh` (image-only deploys that preserve service
  config), `scripts/gcp/verify-p0-production.py` (14-check live P0
  verification), `docs/ops-runbook.md`, `.env.example`.

### Known scope boundaries
- Orgs/teams, Stripe billing, cross-instance rate limiting: product/
  deploy scope, documented in the audit report.
- Frontend CSP keeps `unsafe-inline` for scripts: marketing pages are
  statically prerendered, so per-request nonces are impossible (attempted
  and reverted with evidence in Round 8).

## v6.5.0 — Phase 28: the complete workspace journey (2026-11-14)

Engineering batches 28.1–28.12 executed in order; every batch carries
named, green acceptance evidence on this tree (register:
docs/qualification/phase-28-plan.md).

- **28.1 evidence gate** — stage-bound JUnit names; pilot reader
  requires 3–5 distinct participants and rejects a repeated participant;
  linked-trace content checks for hosted/pilot receipts; duplicate-key
  reasons surfaced; 14 named regressions.
- **28.2 execution controls** — SQLite runner journal carries legacy
  records through restart (in-place migration); durable-cancel before
  owner signal pinned; unreachable owners stay pending; mid-run
  authority/membership revocation refuses dispatch; broker decision
  drills.
- **28.3 browser** — per-node DOM identity token: a rerendered
  duplicate-label decoy can no longer pass the staleness check; iframe
  targets are never resolved onto the main document.
- **28.4 self-building MCP** — worker killed DURING generation and
  BETWEEN validation and activation; a replacement worker completes the
  same build with one effective activation and one parent continuation
  (lease-expiry + redelivery on PostgreSQL).
- **28.5 agents/workflows** — a failed required child blocks parent
  success; an optional child settles NAMED under best_effort
  (`missing` names failed children on every completion policy,
  SQLite + Firestore).
- **28.6 recovery/learning** — the promotion gate compares against the
  DETERMINISTIC recovery-policy floor, not the seeded-random baseline.
- **28.7 usage/frontend** — metered receipts name the RESOLVED provider;
  the governed sync lifecycle works from async dispatch contexts
  (loop-context guard); stale token-card copy replaced with ledger-based
  wording.
- **28.8 deliverables** — exported-file verifier: every source fact must
  survive HTML→PDF conversion; mislabeled/corrupt exports fail.
- **28.9 modeling** — units + material join the versioned spec; a
  dimension edit is measurable in the downloaded geometry; a cancelled
  export publishes nothing.
- **28.10** — tag-only release manifests rejected by the gate.
- **28.11** — full application-restore drill 14/14 on this tree;
  workload measurement 6/6, 100% verified, 0 unauthorized, 0 duplicate
  effects.
- **28.12** — four qualifier attempts retained honestly; journey
  evidence attached; per-target verdicts and open prerequisites recorded.

Final verdict: NOT production-qualified — remaining distance is exactly
attribution in the register (stable-window rerun, B1 hosted credentials,
B4 pilot participants).
