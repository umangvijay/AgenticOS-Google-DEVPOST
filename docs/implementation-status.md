# Implementation status

Snapshot: 31 October 2026. Committed baseline: `3ef8063` (v5.5.0).
This is source status, not a fresh test report. Earlier numerical results belong
to their original revision/environment; see [release evidence](RELEASE_EVIDENCE.md).
Application files are changing concurrently. Recheck diffs before implementation;
this documentation task does not certify those changes.

## Implementation and remaining acceptance

| Area | Present in the reviewed baseline | Remaining acceptance |
|---|---|---|
| Identity | Phase 1 closed and emulator-verified: verified-recipient acceptance, transactional accept/revoke on owned connections (atomic audit), dedicated ownership transfer with the single-owner invariant, background org-membership recheck, access-loss cache clearing in the UI; the SQLite org contract passes on Firestore too (10 parity tests against cloud-firestore-emulator) | — |
| Tenancy | Run org_id and personal-run filtering; adversarial isolation suites; conservative migration rules validated (no retroactive sharing, no ownership repair) | Explicit share/move operations remain a product decision |
| Accounting | Phase 2 complete and emulator-verified: identity-based durable reservations, atomic multi-scope admission, idempotent settlement, reconciliation sweeps, authoritative `/usage/budget` snapshot, metered coverage of every paid path; SQLite and Firestore pass the same contract (7 parity tests against cloud-firestore-emulator); 4 budget E2E journeys pass | — |
| Abuse controls | Bounded local counters | Shared limits, concurrency quotas and overload behavior |
| Workflows | DAGs, leases/fencing, retries and build records | Shared queued build lifecycle and cloud failure drills |
| Side effects | Uncertain quarantine and reconciliation primitives | Provider receipts, controlled resolution and direct-tool coverage |
| Connectors | Trust gate, read evidence, revision/provenance metadata | Accurate coverage, credential-version binding and write/auth tests |
| Standalone MCP | Packaging and protocol-status helpers | Immutable content verification, exact dependencies and isolated runner |
| Browser | Observe/decide/act and challenge pauses | Cleanup, session ownership, action approvals and robust DOM support |
| Learning | UCB-style ranking and epsilon exploration | Verified rewards, persistent evaluations and controlled experiments |
| Frontend | Workspace, session coordination and 3D scene | Real-backend/multi-tab checks, accessibility and state consistency |
| Operations | Build/Terraform configuration and verification scripts | Candidate-bound cloud, scan, restore, rollback and load evidence |

## Working-tree fixes observed at the start of this documentation task

- Legacy direct membership creation changed to a pending-invitation response.
- SQLite usage recording gained a commit call.

These were not newly regression-tested here. Additional implementation may arrive
while documentation is being written. In particular, review transaction ownership
on shared SQLite connections and verify all callers of any reservation API.

## Phase 1 gap closure (31 Oct)

Final §1.0–1.18 pass: audited the checklist against the code and closed the
flagged gaps — ownership transfer (1.8), owned-connection acceptance with the
already-member ordering fix (1.7), the background org-membership recheck
(1.12), frontend access-loss handling (1.13), and migration validation (1.16).
Per-item evidence: [release evidence packet](RELEASE_EVIDENCE.md). Full suite with
the Firestore emulator active: 427 passed, 0 skipped (the emulator run itself
caught a missing Firestore `resend_invitation` and a read-after-write in
`transfer_ownership` — both fixed).

## Phase 2 budget accounting (31 Oct)

Implemented per [budget-accounting.md](budget-accounting.md) and the Phase-2
roadmap §2.1–2.21. The two reproduced defects (first-insert cap bypass,
duplicate settlement) are structurally fixed by the identity-based reservation
contract; evidence per item lives in the
[release evidence packet](RELEASE_EVIDENCE.md).

## Phase 3 admission & capacity (31 Oct)

Implemented per [admission-and-capacity.md](admission-and-capacity.md) and the
Phase-3 roadmap §3.0–3.23, in the prescribed order. The two reproduced defects
(window-ordinal vs epoch cleanup that reset exhausted allowances mid-window;
first-record creation bypassing a zero limit) are fixed and regression-locked
(`test_distributed_rate_limits.py`, including independent-connection and
independent-process evidence). Typed admission decisions (429 vs 503), the
policy inventory with loud unknown-group errors, per-workload outage modes,
shared enforcement on workflow/MCP-build/webhook/schedule/resume/studio paths,
capacity leases with fencing and TTL recovery, durable command admission with
tenant pending caps, bounded local bus with a deadlock-free retry pump, Pub/Sub
flow control, atomic schedule occurrence claims, provider backoff with jitter
and Retry-After, frontend overload states, and the admin admission-metrics
surface. Full backend suite: **493 passed, 0 failed, 21 skipped** (4 new skips are the Firestore-emulator-gated Phase 3 parity suite) (skips are
environment-gated: Firestore emulator / optional providers).

## Phase 6 connector validation & revisions (1 Nov)

Implemented per [connector-validation.md](connector-validation.md) and the
Phase-6 roadmap order. Provenance defaults/preservation, canonical revision
identity, credential-context bindings, superseded validation runs, the
coverage-bypass fix, the eligibility evaluator, atomic activation with race
detection, restore stripping, and the idempotent migration are implemented
and regression-tested (test_connector_phase6.py, 22 tests). Controlled
live-write validation remains gated on §6.2 by design. Full suite:
**545 passed, 0 failed, 27 skipped**.

## Phase 7 standalone MCP contract (1 Nov)

Implemented per [standalone-mcp.md](standalone-mcp.md): the artifact model
with independent capability fields, trusted attestations, the deployment
eligibility service, the runner contract with gated profiles, structured
candidate diagnostics, and complete MCP result preservation (§7.17). The
runner backend proof (§7.5), dependency build pipeline (§7.9), and the
adversarial isolation matrix (§7.23) are the documented next milestones —
managed execution stays explicitly unavailable until a backend is proven.
Full suite: **635 passed, 0 failed, 27 skipped**.

## Phase 8 browser runtime & ownership (1 Nov)

Implemented per [browser-runtime.md](browser-runtime.md): the immediate
§8.4 fixes, the durable session model with separate lifecycle dimensions,
epoch-fenced ownership, separate timers, network policy, credential
injection authorization, material action-intent approvals, effect-ledger
integration, takeover grants, and honest recovery levels
(test_browser_phase8.py, 17 tests). The same-session remote-control
gateway (§8.15) is the documented deployment-side next step. Full suite:
**635 passed, 0 failed, 27 skipped**.

## Phase 9 DOM targeting & verified actions (1 Nov)

Implemented per [dom-targeting.md](dom-targeting.md) and the Phase-9 order:
§9.0 Phase 8 carryover defects closed (tenant CAS, expiry-strict renewal,
monotonic generation, CAS transitions, strict grant expiry, stable
business-action identity, durable sessions wired into live execution), the
structured observation contract, semantic target resolution, staleness
rules, the action pipeline validator, outcome verification, progress
tracking, and concurrency tests. Full suite: **620 passed, 0 failed,
27 skipped**.

## Phase 10 verified ReAct orchestration (1 Nov)

Implemented per [verified-react.md](verified-react.md): structured step
records with evidence digests, untrusted-content separation, bounded loops
across five dimensions, and outcome-gated completion
(test_react_execution.py (17) + test_react_scenarios.py (13) + test_execution_steps.py (4)). Full suite: **635 passed, 0 failed,
27 skipped**.

## Phase 11 recovery episodes (1 Nov)

Implemented per [recovery-episodes.md](recovery-episodes.md): the durable
recovery episode contract with the §11.2 field set, a 12-state state
machine with CAS transitions, atomic ownership claims, persistent limits,
and the §11.0 prerequisite closure (Findings 1–11). Full suite:
**672 passed, 0 failed, 27 skipped** (execution-step persistence wired into engine, recovery episodes wired into worker ticker, recovery API registered).

## Phase 12 evaluation data & outcome verification (1 Nov)

Implemented per [../evals/README.md](../evals/README.md): the evidence
contract scorer (`evaluation_runner.py`) verifying outcomes from
acceptance criteria + the effect ledger + durable execution steps (never
placeholder proxies); the durable `evaluation_records` table and
repository with fixture hashes and pinned scorer versions so fixed
fixtures reproduce identical results and scorer changes cannot rewrite
historical scores; the worker run-finish path now persists records from
REAL effect/step evidence (restart survival); a Firestore parity
repository; the `/api/v1/evaluations/...` router with tenant scoping and
admin-only summary; and the run-workspace EvaluationPanel. Legacy
`EvaluationService` is now a fail-loud adapter over the same core. Full
suite: **688 passed, 0 failed, 27 skipped**.

## Firestore parity for Phase 8–12 durable stores (1 Nov)

Browser sessions, execution steps, recovery episodes, and evaluation
records now have Firestore implementations with the SAME contracts as
SQLite (deterministic document identities, CAS ownership/transition
semantics inside async transactions, SHARED state-machine tables). All
parity suites were run against a LIVE Firestore emulator: **21/21
passed** (Phase 3 admission + Phase 8/10/11 stores + budget). The run
surfaced and fixed two latent defects: the capacity parity test had
never executed against an emulator (missing release; wrong attribute
usage), and the Firestore duplicate-execution-key decision diverged from
the SQLite reference contract (empty worker-id comparison) — aligned.
Full suite: **688 passed, 0 failed, 37 skipped**.

## Phase 5 effect ledger on Firestore (1 Nov)

The durable effect ledger — previously SQLite-only, so any Firestore
deployment failed closed at the effect boundary — now has
`FirestoreEffectRepository` with the same §5.2/§5.5/§5.9/§5.11 contract.
Digest uniqueness is carried by a deterministic anchor document (a query
cannot lock an absence); concurrent duplicate prepares resolve to ONE
effect (proven with a concurrent test on the emulator); CANCELLED_BEFORE_
DISPATCH frees the digest; transitions are version-CAS; receipts are
content-addressed. Full parity suite against a LIVE emulator: **26/26
passed**, including an end-to-end proof (STORAGE_BACKEND=firestore):
effect → OUTCOME_UNKNOWN → honest evaluation record → restart-readable.
Full suite: **688 passed, 0 failed, 42 skipped**.

## Phases 7–21 deep audit (4 Nov, v31.0.0)

The remaining phases audited by their acceptance suites: Phase 7
content-hash binding (altered code → capability replay denial), Phase
8–11 browser/ReAct/recovery suites (112 tests), Phase 16–21
memory/deliverable/evaluations/experiments/governance suites (100+
tests). Full suite: **916 passed, 0 failed, 48 skipped**.

## Phases 3–6 deep verification (3 Nov, v30.0.0)

The second-pass acceptance criteria: the capacity renew/release
`(worker_id='' OR worker_id=?)` bypass replaced with strict ownership
equality on both stores (an imposter could renew another execution's
lease); the webhook replay-key released on failed acceptance (a
delivery's retry identity survives a queue-full rejection). Full suite:
**916 passed, 0 failed, 48 skipped**.

## Phases 1–6 deep audit closure (3 Nov, v29.0.0)

The detailed roadmaps' named defects closed across Phases 1, 4, 5: the
invitation-expiry crash (both stores), the RUNNING→EXPIRED transition
gap, three SQLite-specific reconciler/API queries replaced with
backend-neutral methods on both stores (C12), and the §5.11
reconcile_uncertain CAS guard. Phases 3 and 6 verified by their suites.
Full suite: **911 passed, 0 failed, 48 skipped**.

## Phase 2 deep verification (3 Nov, v28.1.0)

The §2.17 acceptance criteria behind the matrix now have dedicated
tests: overrun recorded-never-clipped + event-trail surfacing, the
dispatching crash window preserving holds with reconciliation-candidate
listing (§2.6: investigation, never unconditional release), uncertainty
staying counted, replay/conflict/idempotency semantics, and fallback
attribution. Full suite: **911 passed, 0 failed, 48 skipped**.

## Phases 1–2 subphase audit (3 Nov, v28.0.0)

The two most detailed original roadmaps audited row-by-row: Phase 1
(1.0–1.18) and Phase 2 (2.0–2.21). All rows verified or integrated; the
explicit matrix gaps closed (role preservation on re-add, UTC
admission-period settlement, negative/zero/reduced-limit rows, settings
semantics). The §2.0 original defects re-probed fixed. Full suite:
**900 passed, 0 failed, 48 skipped**.

## Phase 15 specialized subagents (3 Nov, v27.0.0)

The roadmap's named files: `agent_task_contracts.py` (7-role
SPECIALIST_REGISTRY with boundaries + schemas), the Outcome Verifier
agent (evidence-only, §12.19 combination at the contract boundary) and
Connector Validator agent (case proposals, §6 pipeline executes) — both
running through bounded contracts with typed outputs, dedup, and
lease-fenced claims; the engine delegates needs_review verifications to
the specialist. Full suite: **893 passed, 0 failed, 48 skipped**.

## Phase 14 contextual routing (3 Nov, v26.0.0)

The context-bucketed UCB1 bandit over verified outcomes with logged
selection probabilities, permission-first filtering, a durable
exploration budget (greedy degradation), drift monitoring
(/usage/routing/drift + the operations panel), and the engine/worker
wiring — all 14 roadmap items verified or excluded, fine-tuning
explicitly out of scope. Full suite: **885 passed, 0 failed, 48
skipped**.

## Phase 13 COMPLETE — all 33 subphases (3 Nov, v25.0.0)

The remaining §13.10–13.33: immutable experiment manifests, versioned
draft-proof datasets, stage evidence gates, measured recovery,
connector-revision candidates, specialist contracts, deliverable
verification, governance (metering/redaction/retention), the flagship
"Bring an application" vertical journey (build → bind → test → activate
→ resume → recover → verify → evidence), level-3 API→worker→runtime
proof, and progressive stage flags. 13.24 stays BLOCKED on the
real-browser harness; 13.25's Playwright journey and level-4 staging
remain the recorded next evidence steps. Full suite: **874 passed, 0
failed, 48 skipped**; frontend tsc clean.

## §12.19/§21.3/§12.5/§12.30: judges, replay, recovery intents, pagination (2 Nov)

Model judges restricted to judge-eligible criteria with recorded
provenance and an adversarial labeled harness; inspection-only run
replay re-applying the current policy to durable records; recovery
episode transitions committing outbox intents in the same transaction
(both stores); evaluations retrieval with stable pagination and
evidence-completeness fields. Full suite: **816 passed, 0 failed, 48
skipped** (parity 21/21 live emulator).

## §12.11/§21.3/§21.6/§12.17/§19.6: durable jobs, inbox, comparison, evaluators (2 Nov)

The durable evaluation job queue (create-or-get identity, CAS claims,
visible exhausted parking) with the worker running evaluations through
it; the automation inbox aggregating approvals/failed/needs-review/
uncertain items with precise next actions; evidence-based run
comparison with fixture-hash comparability flags; the task-family
evaluator registry (mutations require provider evidence, never a
non-empty reply); and the shared motion policy. Full suite: **807
passed, 0 failed, 48 skipped**.

## Phases 00/18/20: release scope, frontend state, health (2 Nov)

docs/release-scope.md (supported journeys, exclusions, quality
thresholds) + the extended verify-local gate (source identity,
fresh-database migration check, emulator-gated parity); the frontend
state layer (extended run/outcome model, useRunEvents with
snapshot-first reconciliation and stale-run rejection, draft
preservation, request timeouts); and /admin/operational-health
aggregating queue age, stuck leases, uncertain effects, outbox
backpressure, overdue commands, stale recovery leases, and browser
sweep candidates. Full suite: **797 passed, 0 failed, 48 skipped**.

## Phases 14–17 + 21: routing, subagents, memory, provenance, templates (2 Nov)

Implemented per the 22-phase blueprint: Phase 14 (routing decision log
with verified delayed rewards — an unverified completion can never be a
positive reward — plus coverage-aware offline policy comparison);
Phase 15 (agent task contracts with per-role authority boundaries,
bounded delegation, dedup, claim races, deadlines, and partial-failure
verdicts that name what is missing); Phase 16 (memory lifecycle:
five classes, provenance + read-time expiry, ownership-first retrieval,
supersede-with-history conflicts, correct/export/forget with
propagation); Phase 17 (fact-to-source provenance that flags added
claims and tolerates rephrasing, with content checksums); Phase 21
(versioned workflow templates + side-effect-free dry runs with labeled
cost estimates). Full suite: **796 passed, 0 failed, 48 skipped**
(Firestore parity 21/21 live emulator).

## Phase 13 rollout: executable self-improvement (2 Nov)

The rollout machinery over the Phase 13 core: the versioned prompt store
(immutable versions, activation pointer, pointer-move rollback, SQLite +
Firestore parity), durable routing-policy snapshots with exact restore,
the proposer pipeline turning reviewed failure clusters into
agent-proposed candidates (never fabricating, never self-promoting),
deterministic shadow/canary cohort assignment (reproducible across
restarts, fraction-honoring, shadow never serves the candidate), task
instructions resolved through the store in the engine, and wired
rollback that restores the prior prompt version or policy snapshot —
proven through the full propose→promote→rollback lifecycle. Full suite:
**746 passed, 0 failed, 46 skipped** (parity 19/19 live emulator).

## Review batches 1–2: truthful outcomes + durable contracts (2 Nov)

Implemented the reviewed Phase 12 corrections (findings A–I, §12.0–12.8)
per [REQUIREMENT_LEDGER.md](REQUIREMENT_LEDGER.md): the corrected engine
completion boundary (needs_review parks the run, never claims success);
the evaluation runner gated by verifier verdicts with explicit task_id
joins and the §12.4 effect decision table; authoritative metrics from
the durable usage ledger (both stores) with unavailable ≠ zero;
create-or-get immutable evaluation history in SQLite and Firestore; the
recovery episode as the claim authority (atomic dedup, CAS claims,
owner-generation + live-lease gates, active absolute deadlines); build
context propagation (org scope + credential-binding reference);
the §12.7 capability-ready ladder gating parent resume; the
backend-neutral initial-work contract for both stores; and the honest
EvaluationPanel states. Full suite: **737 passed, 0 failed, 45 skipped**
(Firestore parity 18/18 on a live emulator).

## Phase 13 controlled self-improvement loop (2 Nov)

Implemented per [full-stack-roadmap.md](full-stack-roadmap.md) Phase 13:
the experiment model with per-role CAS state transitions and separated
permissions (agents may propose but never evaluate/approve/promote/roll
back, and never touch authorization/grading targets); durable experiment
records (SQLite + Firestore parity) with pinned dataset and grading
hashes so evaluation cannot be run against a swapped held-out set or
rewritten thresholds; the improvement service with shadow mode, a
bounded canary (predeclared fraction + absolute caps), deterministic
promotion gates (floor/ceiling/cost, cohort fraction, and the critical-
constraint veto that blocks promotion regardless of average quality),
promotion evidence (dataset hash + results + actor + deployment
version), and rollback restoring the prior ref; the admin-gated
experiments API (offline evaluation without a wired evaluator fails
closed with 501); and the dashboard experiments page. Full suite:
**709 passed, 0 failed, 44 skipped** (28 Firestore-emulator parity).

## Earlier focused reproductions

- SQLite usage remained in an open transaction and disappeared on rollback.
  Superseded: accounting mutations now run on dedicated-connection
  `BEGIN IMMEDIATE` transactions and are verified across independent
  connections, real OS processes, and restarts (`test_budget_concurrency.py`).
- Modified standalone server contents still passed mark_deployable after passing
  test statuses were recorded. Content-bound trusted evidence is required.
- Modified operations failed execution validation but still counted as tested
  coverage. Displayed coverage must share the execution predicate.

These focused checks do not replace complete regression suites. The current
[roadmap](full-stack-roadmap.md) provides file maps, acceptance criteria and order.
Prior implementation narratives remain in Git history; this snapshot supersedes
the older blanket “audit items closed” banner.
