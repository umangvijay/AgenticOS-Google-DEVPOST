# Frontend state contracts (Phase 18 §18.8)

Six dimensions tracked SEPARATELY in `frontend/src/lib/run-state.ts` —
combining them is how bugs like "a connection failure turns a running
task into a failed task" happen.

| Dimension | Values | Source |
|---|---|---|
| Execution | queued, running, waiting, recovering, finished, unknown | server task status |
| Outcome | verified, failed, partial, uncertain, needs_review, not_evaluated, unknown | durable evaluation — COMPLETED without one is `not_evaluated`, never verified |
| Connection | connecting, connected, reconnecting, offline, unavailable | online/offline hint (§18.17) + stream health |
| Freshness | current, stale, resynchronizing | last reconcile time vs poll policy |
| Mutation | idle, submitting, accepted, acceptance_unknown, conflicted | command lifecycle (§18.10) |
| Artifact | generated, stored, verifying, verified, stale, delivered, none | deliverable lifecycle + verification.stale (§17.24) |

Deterministic rules:
- Unknown server values degrade to `unknown` — never guessed.
- Terminal states never revert to running (a late running event is
  discarded by `reconcileContradiction`).
- Connection/freshness degrade the VIEW (`degraded: true`), never the
  task's execution or outcome.
- Stale artifact verification renders as `stale` with a warning, never
  an unqualified verified badge.
