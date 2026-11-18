# Event reconciliation (Phase 18 §18.15/§18.16)

## Cursor semantics

Event IDs are OPAQUE STRINGS end to end (the §18.15 fix removed the
integer conversion of `Last-Event-ID` that corrupted UUID cursors into
fresh streams). An unknown cursor falls back to the snapshot — the
defined gap/reset behavior.

## Ordering rules

- Duplicate events: suppressed by a BOUNDED FIFO dedup window (2000
  recent IDs — an ever-growing Set was an unbounded memory leak).
- Late snapshots: the reconciled snapshot never overwrites newer event
  state; execution state derives from the event TYPE via the shared
  contracts (§18.8), then the snapshot confirms from the authoritative
  record.
- Terminal states never revert (see frontend-state-contracts.md).

## Subscription ownership (§18.14)

`run-subscriptions.ts` owns ONE subscription per run (reference
counted). Panels consume it; a coordinated fallback poller runs only
after stream silence (20s heartbeat policy) — never competing with the
stream. Unsubscribe cleans up per consumer; React Strict Mode's double
setup/cleanup cycle is safe.

## Failure handling (§18.16)

Transient stream errors start the coordinated poller; definitive access
loss (401/403/expired) stops the subscription. Retry bounds use jitter.
Streamed error messages never include raw backend exception details.
