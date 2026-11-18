# External Effects & Reconciliation (Phase 5)

> §5.x sections map to the Phase 5 roadmap. Companions:
> [command-lifecycle.md](command-lifecycle.md) (Phase 4),
> [budget-accounting.md](budget-accounting.md) (Phase 2).

Phase 5's core promise:

> AgentOS records each intended external action, preserves uncertainty when
> its outcome is unknown, and retries or resolves it only under a verified
> operation-specific policy.

A timeout does not establish failure. An HTTP success does not always
establish completion. A screenshot does not prove a remote transaction
committed.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Effect identity model + ledger (§5.2–5.4) | **implemented, regression-tested** (`models/effect.py`, `effects`/`effect_receipts` tables) |
| Versioned canonicalization + digest (§5.3) | **implemented, tested** — recipient/target/amount changes are detectable; tenant- and operation-scoped |
| Effect-execution boundary (§5.5/§5.6) | **implemented, tested** (`services/effect_service.py`) — structured outcomes; crash table honored (unknown ≠ failure) |
| Email integration (§5.8) | **implemented, tested with deterministic SMTP simulators** — per-recipient acceptance/refusal, stable Message-ID, partial outcomes, unknown-on-disconnect with NO automatic resend |
| Receipts (§5.9) | **implemented** — provider status, operation id, response digest, redacted detail |
| Evidence-based reconciliation (§5.10) | **implemented, tested** — registered per-operation strategies; absence ≠ proof; empty search preserves UNKNOWN |
| Conditional, audited resolutions (§5.11/§5.12) | **implemented, tested** — expected-version conflicts; operator guesses can close-unresolved but can NEVER manufacture CONFIRMED_NOT_APPLIED |
| Effects & reconciliation API (§5.19) | **implemented** (`/effects/{id}`, `/history`, `/reconcile`, `/resolve`, `/by-run/{run_id}`) |
| Frontend UncertainOutcomePanel (§5.19) | **implemented** — mounted in the workspace; separate "Check outcome" control, never a generic retry |
| MCP tool mutations through the boundary (§5.5) | **documented next integration** (§5.22 rollout step 6) |
| Browser submission effects (§5.14) | **documented next integration** (rollout step 7) |
| Batch/child effects (§5.15) and compensation execution (§5.17) | **model-ready** (`compensates_effect_id`), execution documented as subsequent rollout steps |

## Effect states (§5.4)

`PREPARED → WAITING_APPROVAL? → DISPATCHING → PROVIDER_PENDING? →
CONFIRMED_APPLIED | CONFIRMED_NOT_APPLIED | PARTIALLY_APPLIED |
OUTCOME_UNKNOWN → (evidence only) → CLOSED_UNRESOLVED`; and
`CANCELLED_BEFORE_DISPATCH` before any dispatch intent. Provider-execution
outcome is separate from user cancellation intent, reconciliation status,
and UI notification state.

## Identity (§5.2/§5.3)

`effect_id` (one intended action) is distinct from `effect_attempt_id` (one
transport attempt). A transport retry preserves the effect; a deliberate
second send creates a new one. The request digest is a versioned
canonicalization (sorted keys, preserved array order, content hashes for
attachments, credentials BY REFERENCE) — changing a recipient, target,
attachment, or amount is detectable; identical semantic requests are not.

## The boundary (§5.5/§5.6)

Prepare-or-retrieve → digest compare → dispatch intent committed → provider
call OUTSIDE any DB transaction → receipt + outcome persisted → structured
result. The crash table: a crash after dispatch intent leaves the effect
`DISPATCHING` (potentially dispatched); a lost response preserves
`OUTCOME_UNKNOWN`; receipt-write failure after remote success preserves
uncertainty. No blind retry anywhere: `retry_disposition` is
`retry_items` ONLY for confirmed-not-applied items (§5.16).

## Reconciliation (§5.10–5.12)

Operation-specific evidence strategies are registered per operation;
absence of evidence (empty search) is NOT proof of non-execution —
insufficient evidence preserves `OUTCOME_UNKNOWN`. Resolutions are
conditional on expected state+version in an owned transaction: concurrent
resolvers produce one winner and one conflict. An operator decision without
evidence can `CLOSE_UNRESOLVED` but can never fabricate
`CONFIRMED_NOT_APPLIED` (§5.12's unsafe-replay rule). Email's plain-SMTP
limitation is explicit: there may be NO authoritative automated query
proving whether a timed-out send succeeded — that limitation stays visible
in the UI copy.

## Retry policy (§5.16)

Confirmed applied → never; unknown without a replay guarantee → never;
confirmed not applied → retryable after authorization; partial → eligible
items only, successes preserved. Lower-level transports must not defeat the
policy — SMTP sends are single-attempt at the boundary.

## Tests

`backend/tests/test_effect_dispatch.py` (10 tests) run through the SERVICE
boundary with deterministic SMTP simulators: identity preservation, partial
acceptance, disconnect-unknown, duplicate-no-resend, evidence
reconciliation, empty-search preservation, resolution races, and the
operator-guess rule.
