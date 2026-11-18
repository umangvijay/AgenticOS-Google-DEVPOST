# Command Lifecycle (Phase 4)

> Spec sections referenced as §4.x map to the Phase 4 roadmap. Companions:
> [admission-and-capacity.md](admission-and-capacity.md) (Phase 3),
> [budget-accounting.md](budget-accounting.md) (Phase 2).

Phase 4's central promise:

> Once AgentOS reports a command as accepted, the system can account for
> that command through completion, cancellation, rejection, expiry, or an
> explicitly unresolved outcome — even if an API process, worker, or
> message transport fails.

It does **not** establish exactly-once external effects: email/remote-record
reconciliation remains Phase 5 work.

## Implementation status (honest, per §4.26)

| Capability | Status |
|---|---|
| Command lifecycle model, transition rules, identities | **implemented, regression-tested** (`models/command.py`) |
| Atomic acceptance: command + scoped idempotency + queue accounting + initial work + outbox in ONE transaction (SQLite `BEGIN IMMEDIATE` owned connection) | **implemented, regression-tested** (`repositories/command_repository.py`) |
| Idempotency contract (scoped key, digest conflict, cross-tenant independence, terminal replay) | **implemented, regression-tested** |
| Conditional lifecycle transitions (state-version guard, terminal protection, invalid transitions rejected) | **implemented, regression-tested** |
| Cancellation protocol (intent → conditional states → honest race outcomes) | **implemented, regression-tested**; engine completion is fenced (§4.10) — a lost lease or a won cancellation discards a stale result |
| Outbox dispatcher (lease-claimed batches, mark-after-confirm, bounded backoff, duplicate-republish tolerated) | **implemented, regression-tested**; runs in the worker reconciler pass |
| Command reconciler (deadline expiry, cancellation-ack escalation, outbox drain, parent-wait resolution) | **implemented, regression-tested**; runs each worker tick |
| Parent waits (durable dependency records, resolve-once, reconciler sweep) | **implemented, regression-tested** |
| Webhook ingress on atomic command acceptance | **implemented, regression-tested** |
| Command API (`GET /commands/{id}`, `POST .../cancel`, `POST .../retry`, `GET .../events`) | **implemented** |
| Frontend lifecycle (CommandStatusPanel, CancellationStatus, useCommandStatus; truthful QueueStatusPanel copy) | **implemented** |
| Firestore parity for commands/outbox | **implemented** — `repositories/firestore/command_repository.py` (pre-read transactions, counter-doc caps, deterministic ids, durable capacity-generation records); the 6-test contract matrix runs against the emulator (`test_command_firestore_parity.py`, skips without one — verified-pending-emulator) |
| Build surfaces on atomic command acceptance (§4.12) | **implemented, regression-tested** — API and orchestrator builds commit command + build record + run + task + outbox in ONE transaction; dispatch intent is the outbox event |
| Durable retry/deferral state (§4.11) | **implemented, regression-tested** — `next_attempt_at`/`defer_count` persist on the task row; the reconciler wakes due deferrals (the in-process timer is only a fast path) |
| §4.14 approvals/browser handoff | **implemented** — decision-time authority recheck (disabled accounts cannot approve), digest+expiry binding, atomic compare-and-set resolve; browser restarts report the session NOT recovered and restart browsing honestly |
| §4.17 coordination | **implemented** — queue counting is accepted-not-started (pending AND dispatched); org_id + credential-scope identity propagate into metering and throughput keys (internal references, never raw credentials) |
| §4.21 events/reconnects | **implemented** — lifecycle-critical events persist WITH their transition; SSE honors Last-Event-ID/cursor replay |

## Invariants (§4.1) and where they live

| Invariant | Enforced by |
|---|---|
| Accepted means durable | `accept_command_with_initial_work` — one `BEGIN IMMEDIATE` transaction |
| Publication is recoverable | outbox event commits with the command; dispatcher re-publishes after crash |
| Duplicate requests preserve identity | scoped idempotency key + digest comparison |
| Conflicting reuse is rejected | `IDEMPOTENCY_CONFLICT` receipt (419-style, structured) |
| Duplicate events do not duplicate logical work | command_id primary key + scoped unique + create-if-absent initial work |
| Execution ownership is explicit | Phase 3 capacity leases + §4.10 outcome verdicts (`ACQUIRED_BY_THIS_WORKER` / `ALREADY_OWNED_BY_THIS_WORKER` / `OWNED_BY_ANOTHER_WORKER`) |
| Terminal state is protected | `ALLOWED_TRANSITIONS` + state_version guard |
| Cancellation is truthful | intent recorded; terminal race resolved honestly; ack tracked separately |
| Authorization remains current | `authorize_background_execution` (fail-closed on missing repos, missing owner) runs before every attempt |
| Usage remains attributable | Phase 2 reservations: cancellation/failure settle or mark uncertain |
| Every nonterminal state has an owner | worker consumers + the reconciler's tick |
| UI state is reconstructible | `GET /commands/{id}` + SSE replay (§4.21 reconnects) |

## Lifecycle (§4.3)

States: `ACCEPTED → QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED |
EXPIRED`, with `WAITING` (reasoned: capacity, retry_backoff, child_build,
human_approval, browser_handoff, budget, authorization_review,
outcome_reconciliation) and `CANCEL_REQUESTED`. Transition rules are a
static table (`ALLOWED_TRANSITIONS`); every transition is conditional on the
current state AND the monotonic `state_version` — a stale worker's
completion fails against a newer state. Transport publishing is not a
business state; it lives on the outbox record.

## Idempotency contract (§4.4)

| Request | Result |
|---|---|
| New key | one command created |
| Same tenant/type/key + same digest | the existing command returned (`deduplicated`) |
| Same tenant/type/key + different digest | `IDEMPOTENCY_CONFLICT` |
| Same raw key, other tenant | independent scoped command |
| Retry after terminal | the original terminal state returned |
| Intentional rerun | new idempotency key |

## Acceptance flow (§4.5/§4.6)

```
validate → resolve actor/ownership → scoped key + digest compare
→ queue-admission accounting (same transaction)
→ INSERT command (scoped unique, digest, payload WITHOUT raw credentials)
→ INSERT pending_commands accounting row (accepted-not-started counts
   pending AND dispatched — §4.17's queue definition)
→ initial work (caller callback on the SAME connection; create-if-absent)
→ INSERT outbox event
→ COMMIT → return receipt {command_id, run_id, state, state_version,
                           status_url, deduplicated}
```

SQLITE_BUSY is bounded (`busy_timeout` + BEGIN retry in
`write_transaction`); no external calls happen inside the transaction.

## Delivery (§4.8/§4.9)

The dispatcher claims lease-bounded batches, publishes with the stable
event id, and marks published ONLY after transport confirmation. The crash
window (published, unmarked) results in re-publish — consumers tolerate
duplicates; the command row's state is the deduplication authority. Failures
back off with doubling `available_at` (bounded), releasing the lease so
another dispatcher can recover.

## Cancellation (§4.15)

`record_cancellation_intent` is idempotent and returns terminal commands
unchanged (completion-before-cancellation wins honestly). Duplicate
requests return the same result. The frontend distinguishes requested /
stopping / cancelled / completed-anyway / needs-review
(`CancellationStatus.tsx`). Worker-side cooperative stop checks are the
documented next step (§4.15 steps 3-5).

## Reconciliation (§4.18)

`command_reconciler.reconcile_once` runs each worker tick: expires
deadline-passed commands, escalates unacknowledged cancellations, drains
the outbox, resolves parent waits whose children reached terminal states.
Bounded batches, idempotent repairs, metrics + logs — no blind replay of
uncertain external effects.

## API (§4.19)

`GET /api/v1/commands/{id}` — durable state (the unknown-acceptance
resolver); `POST /api/v1/commands/{id}/cancel`; `POST .../retry` (FAILED
only, new scoped key); `GET .../events`. Structured error codes:
`IDEMPOTENCY_CONFLICT`, `QUEUE_FULL`, `ADMISSION_UNAVAILABLE`,
`AUTHORIZATION_REVOKED`, `COMMAND_EXPIRED`, `INVALID_STATE_TRANSITION`,
`NOT_FOUND`.

## Delivery guarantees shipped in this pass

- **§4.10 renewal + fenced completion**: long-running attempts renew their
  leases on a heartbeat; a lost lease or a won cancellation discards the
  result commit instead of overwriting newer state.
- **§4.16 stale-claim recovery**: the reconciler releases occurrence claims
  whose run never materialized (crash between claim and creation), so
  redelivery can re-claim.
- **§4.21 SSE reconnect contract**: `Last-Event-ID` header / `cursor` query
  replays retained events after the cursor; invalid cursors start fresh.
- **§4.22 ops visibility**: outbox backlog + oldest-unpublished age,
  dispatcher counters, and reconciler repair counts on
  `/admin/admission/metrics`.

## Firestore parity plan (§4.7, pending)

Deterministic doc ids for commands (`commands/{command_id}`), scoped
idempotency records, and outbox events; ONE Firestore transaction performs
pre-reads (scoped key, counters) before writes; strict caps use explicit
counter documents updated in the same transaction; transaction callbacks
never publish. Generation durability for leases uses an execution-record
counter doc so expired leases cannot recycle generations. The contract
matrix (mirroring `test_command_acceptance.py`) runs against the emulator
like the Phase 2/3 parity suites.

## Tests

`backend/tests/test_command_acceptance.py` (13 tests), plus the §4.0D gate
(`test_phase4_acceptance_gate.py`), webhook admission
(`test_webhook_admission.py`), and the existing capacity/admission suites.
Independent connections are used wherever multi-replica is the guarantee.
