# Recovery Episodes (Phase 11)

> §11.x sections map to the Phase 11 roadmap. Companions:
> [verified-react.md](verified-react.md) (Phase 10),
> [browser-runtime.md](browser-runtime.md) (Phase 8).

Goal (§11.0): a recovery flow that survives failure, preserves authority and
effect identity, stays within limits, and proves the original outcome
through the real application path.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Recovery episode contract (§11.2) | **implemented, tested** — `recovery_episodes` table with the full §11.2 field set: identity, original contract, failure classification, proposal, policy, ownership, limits, accounting, effects, outcome |
| State machine (§11.3) | **implemented, tested** — 12 states with per-actor CAS transitions; concurrent transitions → 1 winner + 1 conflict |
| Atomic ownership claims (§11.7) | **implemented, tested** — CAS in BEGIN IMMEDIATE with tenant + lease checks; concurrent claims → 1 winner |
| Restartable recovery (§11.8) | **implemented, tested** — abandoned episodes detected by lease expiry; another worker claims and resumes with an advanced generation |
| Persistent limits (§11.9) | **implemented, tested** — attempt/model_calls/tool_calls/verification_calls counters persist and accumulate |
| Cycle detection (§11.0 Finding 9) | **implemented** — `RecoveryPolicy.decide` accepts action_hash for A→B→A detection |
| Strict action validation (§11.0 Finding 8) | **implemented, tested** — unsupported actions and REPAIR-without-input are explicit bounded rejections |
| Premature success event (§11.0 Finding 11) | **fixed** — TASK_REPAIR_QUEUED (not TASK_RECOVERED) |
| Escalation notification (§11.0 Finding 11) | **fixed** — uses `self.workflow_engine` (was undefined `workflow_engine`) |
| Recovery deadline (§11.0 Finding 7) | **implemented** — `recovery_started_at` passed to the policy |
| Effect-state semantics (§11.0 Finding 2) | **implemented, tested** — CONFIRMED_NOT_APPLIED = FAILED (not verified) |
| Engine completion gating (§11.0 Finding 1) | **implemented** — the engine consults the OutcomeVerifier before marking a task COMPLETED |
| Acceptance criteria persistence (§11.0 Finding 3) | **implemented, tested** — criteria survive save/reload through SQLite |
| Same-session takeover gateway (§8.15) | **designed, not implemented** — requires deployed webRTC/VNC infrastructure |
| Recovery API + frontend panel (§11.17) | **implemented** — `GET /recovery/episodes/{id}` + `GET /recovery/runs/{run_id}/episodes` + RecoveryEpisodePanel.tsx |
| Verified recovery rate metrics (§11.18) | **partially implemented** — the episode table has the fields; `reconcile_orphans` + the worker ticker reconcile abandoned episodes (§11.8) |
| Execution-step persistence in the normal path (§10.5) | **implemented** — engine.execute_task creates a step record per attempt, transitions through READY→VERIFYING→VERIFIED or FAILED, and supersedes on claim failure |

## Tests

`backend/tests/test_recovery_episodes.py` (9 tests) — state machine, atomic
claims, ownership races, CAS transitions, abandoned episodes, persistent
counters. `backend/tests/test_recovery_worker_acceptance.py` (5 tests) —
duplicate delivery, strict action validation, cycle detection, repair
queued event, misleading success. Full suite: **672 passed, 0 failed,
27 skipped**.
