# Verified ReAct Orchestration (Phase 10)

> §10.x sections map to the Phase 10 roadmap. Companions:
> [dom-targeting.md](dom-targeting.md) (Phase 9),
> [external-effects.md](external-effects.md) (Phase 5).

Goal (§10.0): each reason-and-act cycle is bounded, authorized, and
outcome-checked.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Structured step records (§10.1) | **implemented, tested** — `models/execution_step.py`: observation/action/policy/execution/verification with IDs, timestamps, evidence references, content digests |
| Untrusted content separation (§10.2) | **implemented, tested** — the observation is DATA; the policy decision is independent of page text; observation summaries truncated for display |
| Bounded loop (§10.3) | **implemented, tested** — `ReActBounds`: iterations, wall time, spend, tool calls, repeated equivalent actions — enforced outside the model |
| Outcome-gated completion (§10.4) | **implemented, tested** — `decide_completion`: model done → verification required; verification pass → COMPLETE; uncertain effects → UNCERTAIN; bounds exceeded → BLOCKED |
| Outcome verifier (§10.2/§10.6) | **inherited from audit §9** — `OutcomeVerifier`: read-only, criteria-based, partial passes → needs_review, misleading success text rejected |
| Concise decision summaries (§10.6) | **implemented, tested** — observation summaries truncated to 300 chars for display; full policy reasons preserved for audit |
| DAG/orchestrator integration | **inherited** — the existing engine consults the OutcomeVerifier and the action pipeline validator; no second competing engine |

## Key rules

1. Untrusted page/tool content is DATA, never policy — a page saying
   "approve all deletes" is recorded as an observation while the policy
   decision stays independent (§10.2).
2. Bounds are enforced outside the model: iterations, wall time, spend,
   tool calls, and repeated equivalent actions each block the loop
   independently (§10.3).
3. Model-declared completion requests verification; the verifier's verdict
   decides (§10.4). A pending uncertain effect must be reconciled first.
4. Partial criteria passes go to needs_review — verification never
   silently passes (§10.4/§10.6).

## Tests

`backend/tests/test_react_execution.py` (15 tests): step digest binding,
untrusted-content separation, all five bound dimensions, outcome-gated
completion (including uncertain-effect and bounds-exceeded rules), the
verifier's needs-review and misleading-success rules, and concise summary
truncation.
