# DOM Targeting & Verified Browser Actions (Phase 9)

> §9.x sections map to the Phase 9 roadmap. Companions:
> [browser-runtime.md](browser-runtime.md) (Phase 8),
> [external-effects.md](external-effects.md) (Phase 5).

Goal (§9.1): AgentOS observes a page, identifies the intended control
semantically, checks that it is still the correct target, performs an
authorized action, and verifies the expected result. Ten invoices with ten
Delete buttons: the user authorizes deleting INV-1042 — AgentOS must find
THAT row's button, re-observe if rows reorder, and never click whatever now
occupies a previous position.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Structured observation contract (§9.3) | **implemented, tested** — `models/browser_observation.py`: semantic roles, accessible names, containers, business identities, locator candidates; bounded SCOPED observation with explicit truncation info (never "target absent" because it was outside the batch) |
| Semantic target resolution (§9.4) | **implemented, tested** — resolution order: role+name in container → label → test id → business identity → structural; ambiguity REPORTED (never .first/.nth(0)); business-identity scoping for repeated controls |
| Staleness rules (§9.5) | **implemented, tested** — document/navigation/frame/page/controller/owner/material changes each invalidate; unrelated animation does NOT; structured failures (STALE_OBSERVATION, TARGET_MISSING, TARGET_AMBIGUOUS, TARGET_IDENTITY_CHANGED, FRAME_REPLACED, ORIGIN_CHANGED, CONTROL_CHANGED) |
| Action pipeline (§9.6) | **implemented, tested** — `validate_browser_action` enforces ownership epoch, session state, controller, argument schemas (no JS/selector injection), staleness, origin, approval-digest match, and effect-recording for consequential actions |
| Outcome verification (§9.8) | **implemented, tested** — three levels (interaction completed / application acknowledged / business verified); writes without business evidence → OUTCOME_UNCERTAIN (reconcile, never retry); model "done" ≠ verification |
| Progress tracking (§9.17) | **implemented, tested** — meaningful progress signals (milestones, state changes); repeated equivalent actions, ambiguity, empty observations, and uncertain submissions each bound and detected |
| Durable sessions in live execution (§9.0A) | **implemented** — web_agent allocates a durable session record before launching; local handles bind to session_id + epoch; cleanup closes the record; the worker sweeps expired sessions |
| Tenant CAS + monotonic generation (§9.0A) | **implemented, tested** — tenant enforced in the acquisition transaction; server-controlled monotonically advancing fencing generation (a caller cannot reuse an epoch by repeating an attempt) |
| Stable business-action identity (§9.0D) | **implemented, tested** — material digests EXCLUDE the ownership epoch: recovery keeps the same effect; a different payload gets a NEW effect |
| Concurrency (§9.21) | **implemented, tested** — four-worker concurrent races → 1 winner (asyncio.gather, not sequential calls) |
| Strict grant expiry (§9.0A) | **implemented, tested** — dead at the exact expiration timestamp; tenant + epoch must match |
| Real-browser DOM fixture journeys (§9.20) | **designed; requires the browser fixture harness** — the resolution/staleness/pipeline logic is tested through the services the real call chain uses; Playwright-level fixture runs need the local browser runtime |
| Playwright locator execution (§9.4 runtime step) | **partially wired** — the resolver returns locator strategies; the executor maps them to Playwright locator API calls in the browser runtime |

## Key rules

1. Position is not identity: business identity + semantic container scope
   repeated controls (§9.4).
2. Stale observations cannot execute — each staleness dimension has its own
   structured failure (§9.5).
3. The model proposes intent; it cannot issue unrestricted selectors,
   JavaScript, filesystem paths, or network permissions (§9.6).
4. Writes without business evidence are OUTCOME_UNCERTAIN — reconcile
   before retrying (§9.8).
5. Page content is untrusted input: the observation model view is
   redacted and structurally separate from system instructions (§9.18).
