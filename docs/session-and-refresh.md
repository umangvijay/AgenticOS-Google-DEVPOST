# Session and refresh architecture

## Decision (§18.13)

The release RETAINS the current token client (access + refresh tokens in
localStorage, Authorization header, CSRF for writes). A migration to an
HttpOnly-cookie/BFF design is NOT part of this phase: the API contract,
streaming (SSE via fetch-event-source), downloads, and OAuth callbacks
all assume header-based auth, and a partial migration would leave token
ownership unclear. The decision is revisited only with a complete
migration plan covering the §18.13 checklist.

## Session states (§18.11)

`auth-state.ts` defines: initializing, authenticated, refreshing,
temporarily_unavailable, expired, signed_out. Definitive rejection
(401/403) ends the session; network/5xx failures are TEMPORARY and keep
the UI functional with stale data.

An authentication GENERATION identifier increments on login/logout/
account switch. Async responses carry their generation; stale
generations are discarded (a delayed account-A response can never
populate account B's screen).

## Cross-tab refresh (§18.12)

- **With Web Locks**: `navigator.locks.request` guarantees a single
  owner for the rotation window.
- **Without Web Locks** (Firefox < 96, older Safari): a localStorage
  lock with a TTL. The non-owner NEVER rotates the single-use refresh
  token — it waits briefly and RE-READS the session the owner rotated.

Tested scenarios: two simultaneous refreshes, tab closing mid-refresh,
suspended tab returning, late success after another tab's rejection,
logout during refresh, account switch during refresh, ambiguous
responses.

## Logout contract

Logout stops subscriptions, cancels pending client work, clears
protected caches (session, drafts for the signing-out user via
`clearAllDraftsForUser`), and broadcasts `AUTH_CHANGED` to other tabs.
