# Verified ReAct Orchestration (Phase 10)

> §10.x sections map to the Phase 10 roadmap. Companions:
> [dom-targeting.md](dom-targeting.md) (Phase 9),
> [browser-runtime.md](browser-runtime.md) (Phase 8),
> [command-lifecycle.md](command-lifecycle.md) (Phase 4).

Goal (§10.3): each reason-and-act cycle is bounded, authorized, and
outcome-checked. The AgentOS cycle: Observe → Propose → Validate →
Authorize → Reserve resources → Dispatch → Observe result → Verify →
Continue, pause, or finish.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Workflow acceptance dedup + digest (§10.1) | **implemented, tested** — dedup returns the original run; different payload conflicts; canonical digest |
| Firestore initial-work contract (§10.1) | **implemented, tested** — the service detects the repo type and refuses SQLite callbacks into Firestore with a clear contract error |
| Durable execution-step records (§10.5) | **implemented, tested** — `execution_steps` table with the §10.5 field set (logical action, attempt, command/run/task/tenant, policy revisions, evidence, effect ID) |
| Step state machine (§10.6) | **implemented, tested** — 13 states with CAS transitions (expected state + version in the UPDATE); concurrent cancellation/completion → one winner + one conflict |
| Step evidence (§10.6) | **implemented** — evidence references appended atomically inside the transition transaction |
| Permissive verifier fix (§10.8) | **implemented, tested** — no criteria = needs_review (not pass); artifact_exists requires a RESOLVER (unresolvable = needs_review; nonexistent = fail); a generic click with only interaction evidence = OUTCOME_UNCERTAIN |
| Credential staleness (§10.23) | **implemented, tested** — credential-binding changes invalidate browser context |
| Bounded loops (§10.3) | **implemented, tested** (Phase 9 `ReActBounds` + `ProgressTracker`) |
| Untrusted content separation (§10.16) | **implemented, tested** (Phase 9 §9.18) |
| Outcome verifier (§10.8) | **inherited from audit §9, hardened per §10.8** |
| DAG/orchestrator integration (§10.18) | **inherited** — the existing engine consults the OutcomeVerifier and the action pipeline validator |
| Per-model-call ADK metering (§10.11) | **implemented** (Phase 2 §4.0C: each ADK attempt holds its own reservation) |
| Cancellation preserving uncertainty (§10.15) | **implemented** (Phase 4 §4.15 + Phase 8 §8.14) |
| Frontend execution view (§10.21) | **partially implemented** — the workspace mounts command/effect panels; the full step-level execution view lands with the ReAct coordinator integration |
| React coordinator (§10.3) | **designed** — the existing DAG engine + the Phase 9/10 validators are the pipeline; the coordinator service is the next integration step |
| Acceptance criteria contract (§10.7) | **implemented, tested** — Task carries acceptance_criteria/requested_outcomes/allowed_action_scope/completion_policy; write tasks REQUIRE effect_resolved/artifact_exists criteria; the application validates that model-proposed criteria do not weaken mandatory requirements |
| Evidence service (§10.8) | **implemented, tested** — `evidence_service.verify_task_outcome`: effect-ledger states checked first (uncertain → OUTCOME_UNCERTAIN); artifact_exists requires a resolver; model-claimed-done limitations surfaced on all non-verified paths |
| E2E scenarios (§10.22) | **implemented, tested** — 13 deterministic scenarios covering the §10.22 matrix (misleading success, fabricated artifacts, uncertain effects, prompt injection, loop limits, budget exhaustion, duplicate delivery, crash-recovery, cancellation races) |

## Key rules

1. A deduplicated acceptance returns the ORIGINAL run — never replans a
   new one (§10.1).
2. Same key + different payload → IDEMPOTENCY_CONFLICT (§10.1).
3. No acceptance criteria = needs_review, not a silent pass (§10.8).
4. A generic click with only interaction evidence = OUTCOME_UNCERTAIN
   (§10.8) — a click can delete, publish, or submit.
5. DISPATCHING → OUTCOME_UNCERTAIN when the result is lost; a retry does
   not convert that into another dispatch (§10.6).
6. Cancellation races completion → one CAS winner (§10.15).
7. The material action digest excludes the ownership epoch — recovery
   keeps the same effect; a different payload gets a new one (§9.0D).
