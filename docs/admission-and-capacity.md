# Admission & Capacity (Phase 3)

> Spec sections referenced as §3.x map to the Phase 3 review roadmap
> (3.0–3.23). Companion docs: `docs/budget-accounting.md` (Phase 2 spending
> control), `docs/AUTHORIZATION_INVENTORY.md` (Phase 1).

Phase 3 answers one question: **can the system safely accept and execute
this work now?** — separately from Phase 1 (is this user allowed?) and
Phase 2 (does this action have allowance?). It covers distributed rate
limiting, concurrency control, durable queue admission, backpressure, and
fairness.

## Guarantees (§3.1)

1. A user cannot multiply their allowance by reaching different API replicas.
2. A tenant cannot monopolize execution capacity.
3. Expensive work cannot create an unlimited queue.
4. A browser worker cannot launch more browsers than its supported capacity.
5. Provider calls remain within configured throughput limits.
6. Retries cannot create a new overload wave.
7. Duplicate execution events do not acquire duplicate capacity.
8. Capacity is released after success, failure, cancellation, and worker loss.
9. A limiter outage produces explicit, policy-defined behavior.
10. The frontend explains whether work is rejected, queued, throttled, or accepted.

## The control inventory (§3.2) — one resource, one control

| Control | Implementation | Protects |
|---|---|---|
| Request rate (in-process) | `security/rate_limiter.py` sliding window | each API instance |
| Request rate (shared) | `security/shared_rate_limiter.py` fixed window in SQLite/Firestore | cross-replica allowance |
| Execution concurrency | `services/capacity_service.py` + `repositories/capacity_repository.py` leases | active tasks/builds/browsers |
| Queue admission | `services/admission_service.py` + `repositories/admission_repository.py` | pending-command caps |
| Provider throughput | `provider_request` policy enforced in `_metered_attempt` | provider credentials |
| Spending | Phase 2 `budget_service` (unchanged) | tokens/USD |
| Local backpressure | bounded `in_memory_message_bus` | process memory |

One "requests per minute" number never substitutes for another control.

## Policy inventory (§3.3)

`backend/security/admission_policy.py` is the authoritative mapping from
entry point to policy: limit, window, identity scope, algorithm, shared
enforcement, failure mode, and policy version. Unknown groups raise
`AdmissionPolicyError` — a configuration error is loud, never a silent fall
back to `general_read`. Legacy group strings (`auth`, `contact`, `webhook`,
`workflow`, `mcp_build`, `general`, `resume_analyze`, `resume_tailor`,
`capabilities`) are aliases that resolve to canonical policies and share one
counter with them.

Settings carry the numbers: `RATE_LIMIT_*` in `backend/config/settings.py`.
Fail-closed groups (`workflow_create`, `integration_build`,
`browser_start`) refuse with **503 ADMISSION_UNAVAILABLE** when the shared
store is down (§3.9): a store outage must not quietly multiply strict
limits by replica count. Fail-open groups degrade to the per-process
limiter, labeled `local_emergency`/`degraded` in the decision and counted
in `local_fallback_activations` metrics.

## Shared window representation (§3.4) — the v5.x defect, fixed

Pre-Phase-3, `current_window()` returned a **window ordinal**
(`epoch // 60 ≈ 29M`) while the worker purged with an **epoch** cutoff
(`≈ 1.79B`): cleanup classified every active window as expired and reset
exhausted allowances mid-window. The representation is now epoch seconds
end-to-end:

```
window_start = floor(now_epoch / window_seconds) * window_seconds
window_end   = window_start + window_seconds
cleanup_after = window_end + window_seconds     # one retention window
```

- The interval is part of the key — different interval configurations never
  collide on one counter.
- `purge_expired` deletes only rows with `cleanup_after < min(cutoff, now)`,
  in batches of 500 — a live counter is structurally undeletable, whatever
  cutoff a caller passes.
- Existing databases are migrated by `_migrate_rate_limit_schema`
  (ephemeral counters are recreated; in-flight windows relax once).
- Firestore: doc ids include the interval; native TTL, if configured, is
  complementary only — admission always interprets the stored window.

**First acceptance test** (regression-covered): cleanup runs during an
active exhausted window → another request remains rejected.

## First-record admission (§3.5)

Negative limit / invalid interval → `ValueError` in both backends. Zero
allowance → explicit `DISABLED_BY_POLICY` refusal BEFORE any record is
created (the pre-Phase-3 first-record bypass admitted limit-0 identities).
Empty/malformed identity → `POLICY_INVALID`, never an accidentally
unlimited bucket. Admission is a single atomic upsert (SQLite) or
transaction (Firestore) — no application-level read-then-write.

## Typed decisions (§3.8)

`backend/models/admission.py::AdmissionDecision` replaces the ambiguous
`True/False/None` triple. Fields: `allowed`, `reason` (`RATE_LIMITED`,
`TENANT_CAPACITY_EXHAUSTED`, `QUEUE_FULL`, `PROVIDER_THROTTLED`,
`ADMISSION_UNAVAILABLE`, `POLICY_INVALID`), `enforcement` (`shared`,
`local_only`, `local_emergency`, `disabled_by_policy`), `limit`,
`remaining`, `retry_after_seconds`, `reset_at_epoch`, `decision_id`,
`degraded`. HTTP mapping: 429 for a healthy quota denial, **503** when
admission itself is unavailable. The error body is the frontend contract:

```json
{
  "code": "RATE_LIMITED",
  "message": "Too many requests in the current interval.",
  "group": "workflow_create",
  "retry_after_seconds": 37,
  "accepted": false,
  "enforcement": "shared",
  "decision_id": "…"
}
```

## Capacity leases (§3.11)

`capacity_leases` (SQLite/Firestore) count ACTIVE work per
`(resource_class, tenant)` — `workflow_task`, `mcp_build`,
`browser_session`, `provider_call`, `artifact_generation`. Per-tenant caps:
`TENANT_MAX_ACTIVE_TASKS/BUILDS/BROWSERS`. Properties, all regression-tested:

1. Atomic acquisition (guarded single INSERT; racing coroutines and
   independent connections/processes cannot over-admit).
2. Duplicate execution events re-acquire the SAME lease (partial unique
   index on `(resource_class, scope_key, execution_key) WHERE state='active'`).
3. Release is idempotent; `release_execution` covers resumed sessions.
4. Renewal verifies ownership (worker id + generation).
5. Fencing: every re-acquire bumps `generation`; stale generations cannot
   renew or release the current lease.
6. Each machine is separately bounded by a per-process semaphore.
7. Shared leases bound cross-instance totals.
8. Worker loss: TTL expiry + `reconcile_expired` (worker ticker, ≤10k rows
   per sweep) reclaim capacity.

§3.11's warning is honored: expiration cannot PROVE a partitioned worker
stopped. Fencing blocks a zombie's commits; strict physical bounds (browser
hard-kills) remain documented operational concerns.

Engine wiring (`engine.execute_task`): capacity is acquired BEFORE the task
claim; a tenant at cap defers through the bus with exponential backoff
(capped 300s, jittered — no retrigger storm); release happens in `finally`
— success, failure, timeout, cancellation, and approval-waiting all free
the slot. Browser wiring (`web_agent.run`): a NEW Chromium acquires a
`browser_session` lease; paused HITL sessions keep holding it (the browser
is retained); resumed sessions release by execution key on close.

## Durable queue admission (§3.12)

`pending_commands` is the strict accepted-work counter — never a Pub/Sub
backlog approximation. Caps: `TENANT_MAX_PENDING_COMMANDS` per user (×4
org, ×8 class), max wait clamped to 6h, lifecycle
`pending → dispatched → settled | expired`. Duplicate submissions (same
`command_id` or idempotency key) return the EXISTING admission instead of
consuming a slot. MCP builds admit through this at the `_start_build`
funnel; a persistence failure settles the command so no slot is stranded.
Expired rows are swept by the worker ticker.

## Local bus bounds (§3.13)

Topic queues are bounded (`LOCAL_QUEUE_MAXSIZE`); publish waits up to
`LOCAL_QUEUE_PUT_TIMEOUT` for space then raises `MessageBusFullError` —
rejection, never silent growth. The retry path no longer sleeps inside the
consumer and re-`put`s (deadlock with bounded queues): retries park in a
bounded delayed-retry store (`LOCAL_MAX_PENDING_RETRIES`, age-capped by
`LOCAL_MAX_RETRY_AGE_SECONDS` → dead-letter), moved by a single pump task
that never blocks. Dead letters are bounded (`LOCAL_DEAD_LETTER_MAXSIZE`,
oldest dropped). The pump is cancelled with the consumers — no orphaned
requeues. Local-mode semantics stay explicit: no restart durability; Pub/Sub
is the durable deployment path.

## Pub/Sub flow control (§3.14)

The subscriber passes explicit `FlowControl(max_messages=
PUBSUB_MAX_OUTSTANDING_MESSAGES, max_bytes=PUBSUB_MAX_OUTSTANDING_BYTES)`;
each callback blocks its subscriber thread until the handler settles, so
delivery cannot race ahead of execution. nacks redeliver after the ack
deadline — a bounded defer, not a tight loop.

## Schedule & webhook admission (§3.15)

`schedule_occurrences` makes the trigger atomic: the claim INSERT on
`(schedule_id, occurrence)` resolves duplicate deliveries/redeliveries to
exactly one logical execution; the loser acks as a duplicate skip. A crash
between claim and run-creation RELEASES the claim so redelivery can retry.
The history check remains as the overlap-policy notification layer
(`SKIPPED_BUSY` + user notification). Webhooks: signature verification,
replay dedup (idempotency ledger), and IP + owner admission (both shared).

## Fairness & provider protection (§3.16)

Per-tenant active caps are the first fairness control; separate workload
classes (`workflow` / `build` / `webhook` / `schedule`) bound each class's
pending share. Provider throttling: bounded exponential backoff with ±25%
jitter, Retry-After honored (clamped), fresh Phase 2 reservation per
attempt. Recovery and subagents go through the same engine path — same
capacity, admission, and budget controls as user-triggered work.

## Coordination with Phase 2 (§3.17)

Ordering in `_metered_attempt` and `execute_task`:

```
authorization (route) → request admission → command admission (builds)
→ persist accepted work → capacity lease → budget reservation → execute
→ settle usage → release capacity
```

A budget refusal after capacity acquisition releases the slot in `finally`;
a capacity refusal never starts paid execution. Waiting-for-approval tasks
do not hold a worker slot; a paused HITL browser still holds its browser
lease.

## Edge & platform layers (§3.18)

Cloud Run request concurrency is NOT workflow concurrency: one HTTP request
can create durable work that outlives the response. The `cloud_run` module
now exposes an explicit `ingress` variable — when Cloud Armor fronts a load
balancer, set `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` so the default
`*.run.app` URL cannot bypass the edge. Cloud Armor thresholds apply per
backend service and are no substitute for the exact per-organization quotas
implemented here.

## Frontend states (§3.19)

`AdmissionNotice.tsx` renders the overload states (rate limited, capacity
exhausted, queue full, provider throttled, admission unavailable, policy
invalid) with real Retry-After countdowns and a retry action — the draft is
preserved on refusal. `QueueStatusPanel.tsx` shows accepted work only,
backed by real identifiers — it never invents queue positions. `api.ts`
exposes `AdmissionErrorBody`, `isAdmissionError`, `admissionBodyFrom`, and
`jitteredDelay` (SSE reconnects are jittered). Repeated submits are gated
by the composer's loading state.

## Metrics (§3.20)

`GET /api/v1/admin/admission/metrics` (admin-gated) aggregates:
admissions/denials per policy, shared-store errors, **local fallback
activations**, capacity denials, lease expiries, command
admissions/rejections/duplicates, and the full policy inventory. Labels
carry group/class/reason only — never user or command ids; correlation
lives in access-controlled logs via `decision_id`. The two load-bearing
alerts: cleanup deleting active-window records (now structurally
impossible, still observable via cleanup volume) and local fallback
activation (strict enforcement degraded).

## Completion closure — every entry point gated

The §3.10 path table, completed. Admission lives at the SERVICE funnel so
every surface is covered without per-route drift:

- **Agent-requested builds (§3.16)**: `request_durable_build` and
  `track_inline_build` run the SAME gate (`admit_build`) — shared
  `mcp_build` allowance plus durable command admission with the source-hash
  idempotency key. The orchestrator surfaces a denial as an honest,
  non-retriable refusal ("Do not retry immediately"), never a retryable
  error the model can loop into the same cap. The router maps the typed
  `BuildAdmissionDenied` to 429/409.
- **MCP_BUILD capacity leases**: `core.mcp_build` tasks hold BOTH a
  `workflow_task` and an `mcp_build` lease (consistent task→build
  acquisition order, §3.17); a tenant at `TENANT_MAX_ACTIVE_BUILDS` defers
  like any other capacity refusal.
- **SSE connection capacity (§3.2)**: the `/workflows/{id}/events` stream
  enforces the `event_stream` policy (in-process by design).
- **Webhook command admission (§3.15)**: a delivery is an owner COMMAND —
  per-user pending caps apply, replays are refused by the idempotency
  ledger, and a settled command frees its idempotency key for genuine new
  work. A queue cap on the owner returns QUEUE_FULL to the sender.
- **Metrics depth (§3.20)**: shared-store admit latency (early degradation
  signal), ACTIVE lease units by resource class, per-class pending depth
  with OLDEST WAIT, and provider retry volume (labels: provider + error
  class only) — all on `GET /admin/admission/metrics`.
- **Firestore parity (§3.21)**: `test_phase3_firestore_parity.py` runs the
  shared-limit, capacity-lease, and command-admission matrix against the
  Firestore emulator (`FIRESTORE_EMULATOR_HOST=localhost:8080
  GOOGLE_CLOUD_PROJECT=demo pytest backend/tests/test_phase3_firestore_parity.py`);
  skipped cleanly without one, mirroring the Phase 2 parity suite.
- **Frontend coverage (§3.19)**: AdmissionNotice wired into the dashboard
  composer, the integration-create page (input preserved), and the
  schedules page (run-now refusals with retry); QueueStatusPanel mounted on
  the workspace page.

## Cancellation and local permit ownership (Phase 26)

Local permits are keyed by resource class, tenant scope and execution key.
They retain the acquiring semaphore and the exact lease ID, worker, generation
and owner. Duplicate pending/active execution defers before a second shared
admission. Distinct tenants may use the same execution key independently.
Cancellation or failure during shared admission returns the local permit,
including cancellation before dispatch while preparing an execution step,
claiming the task, recording its start or synchronizing command state.

Late, duplicate or foreign cleanup cannot release a replacement permit. Browser
resume cleanup by execution key requires a locally held lease and uses that
lease's original shared-store fence; it cannot release another worker's lease
by key. If the outcome of an interrupted shared write is unknown, its durable
lease remains subject to TTL/reconciliation. This does not prove an orphaned
external process has stopped.

Local guards belong to one event loop per resource class in a worker process.
A second live loop defers rather than creating an additional set of permits.
A replacement loop can retire bookkeeping from a closed loop, and late cleanup
never returns an old permit to the replacement's semaphore. Run independent
worker loops in separate processes. Shared repositories still enforce tenant
capacity across those processes.

## Test evidence (§3.21)

- `test_distributed_rate_limits.py` — the two original defects, zero-limit
  first records, racing allowance, cleanup-vs-active-window, interval keys,
  independent connections AND independent OS processes (subprocess).
- `test_admission_policy.py` — inventory totality, alias unification,
  typed decisions + HTTP contract, all four enforcement modes, fail-open vs
  fail-closed outages.
- `test_capacity_leases.py` — atomicity, tenant isolation, idempotent
  re-acquisition, fencing, TTL recovery, slot lifecycle, defer backoff.
- `test_queue_backpressure.py` — tenant pending caps, duplicates, expiry,
  class caps; bounded bus rejection, retry-store overflow, DLQ bounds, the
  full-queue retry deadlock scenario, shutdown with pending messages.
- `test_schedule_admission.py` — atomic occurrence claims incl. two
  connections racing one claim, release-on-crash, duplicate-trigger handler.
- `test_provider_backoff.py` — bounded growth, Retry-After, jitter.
- Updated: `test_shared_limits_and_browser_verification.py` (epoch windows,
  typed API).

Independent clients/processes are used wherever a guarantee concerns
multiple instances — two repository objects over one connection prove
nothing.
