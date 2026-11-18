# Browser Runtime, Ownership & Recovery (Phase 8)

> §8.x sections map to the Phase 8 roadmap. Companions:
> [external-effects.md](external-effects.md) (Phase 5),
> [command-lifecycle.md](command-lifecycle.md) (Phase 4).

Goal (§8.0): every browser session has an owner, a bounded lifetime, one
active controller, enforced permissions, and a clear recovery path.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Immediate execution fixes (§8.4) | **implemented, tested** — awaited page-state capture; done decisions recorded separately from executed steps (outcome verification over executed steps only); every browser entry point requires execution context (the orchestrator refuses without run_id/task_id) |
| Durable session model (§8.5) | **implemented, tested** — `browser_sessions` table + `models/browser_session.py`; durable metadata separated from process-local Playwright handles |
| Separate lifecycle dimensions (§8.6) | **implemented, tested** — resource/controller/challenge/cleanup as independent enums with a permitted-transition matrix validating the actor |
| Ownership leases + epoch fencing (§8.7) | **implemented, tested** — CAS acquisition (racing workers → 1 winner), epoch-based fencing (old worker's renewal fails after takeover), duplicate resume resolves to the SAME session |
| Bounded cleanup (§8.9) | **inherited** — capacity lease release on all paths (Phase 3), sweep for expired sessions, restored-session cleanup |
| Separate timers (§8.10) | **implemented, tested** — idle, handoff, absolute lifetimes as independent columns; activity extends idle but NEVER absolute |
| Network policy (§8.11) | **implemented, tested** — navigation vs resource vs websocket categories with normalized origins; alternate ports are distinct origins |
| Credential injection authorization (§8.12) | **implemented, tested** — origin/frame/purpose/controller/challenge/observation checks; challenge fields are human-only regardless of controller; page-instructed wrong-purpose injection DENIED |
| Action intents (§8.13) | **implemented, tested** — material digests bind approval to target+payload+session+epoch; non-form writes (delete/publish/settings/autosave) classified consequential |
| Effect ledger integration (§8.14) | **implemented, tested** — browser effects recorded before dispatch; restart with OUTCOME_UNKNOWN requires reconciliation (no silent repeat) |
| Takeover grants (§8.16) | **implemented, tested** — short-lived, epoch-bound, view ≠ control, revocation closes control |
| Recovery levels (§8.19) | **implemented, tested** — live continuation / authenticated reconstruction / reauth / manual reconciliation / unsupported, decided honestly |
| Human takeover gateway (§8.15) | **designed, not implemented** — the same-session remote-control gateway requires deployed infrastructure (webRTC/VNC-over-gateway); local headed windows continue to work with the ownership model |
| Frontend BrowserSessionPanel (§8.22) | **partially implemented** — session state, controller, and effect panels mounted in the workspace; the full supervision table lands with the gateway |
| Tabs/frames/downloads limits (§8.21) | **designed** — the model carries page/cleanup tracking; the runtime enforcement lands with the gateway |
| Semantic observation in the action loop (§9.3/§9.4) | **implemented** — the snapshot captures role, accessible name, label, container identity, and test IDs; the Phase 9 resolver builds the observation model |
| Action validation in the loop (§9.6) | **implemented** — `validate_action_arguments` runs before dispatch; invalid arguments are skipped with structured errors |
| Material action-intent binding (§8.13) | **implemented** — consequential actions build an `ActionIntent` with a material digest covering target+payload+session; the approval binds to this digest, not URL+prose |
| Effect recording for browser actions (§8.14) | **implemented** — consequential browser actions are recorded in the effect ledger before dispatch (restart cannot silently repeat) |

## Key rules

1. Every browser entry point requires execution context; missing context
   stops BEFORE the action (§8.4 A).
2. Model decisions are separate from executed actions; completion
   verification runs over EXECUTED steps only (§8.4 C).
3. Approvals bind to the MATERIAL action digest (target + payload + session
   + epoch) — any material change invalidates prior approval (§8.13).
4. Uncertain browser outcomes are preserved: a crash after submit leaves
   OUTCOME_UNKNOWN, requiring reconciliation — never automatic resend
   (§8.14).
5. Heartbeats never extend the absolute session lifetime (§8.10).
6. OTP/MFA/re-auth fields are human-only regardless of controller (§8.12).

## Tests

`backend/tests/test_browser_phase8.py` (17 tests) — ownership races, epoch
fencing, transition matrix, controller distinction, timers, network policy,
injection authorization, material binding, effect integration, grant
lifecycle, recovery levels.
