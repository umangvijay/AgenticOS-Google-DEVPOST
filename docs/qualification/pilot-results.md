# Real-User Pilot Results (Phase 22 §22.27)

**Status: PENDING EXECUTION — no real participants were available during
the automated qualification window (blocker B4).** This file MUST NOT be
filled from automated runs, synthetic accounts, or agent-driven
sessions; see [pilot-plan.md](pilot-plan.md) for the consented task
script, preconditions, and pre-declared success criteria.

## Preconditions checklist at freeze time

| Precondition | State |
|---|---|
| Isolation gates (`test_phase21_isolated_runner.py`) | PASS 12/12 |
| Authorization/tenancy live probes | PASS 18/18 (`data/phase22_live_probes.json`) |
| Token revocation on logout (S1) | FIXED + re-verified live (401) |
| Feedback lifecycle gates | PASS (suite + e2e) |
| Deliverable integrity gates | PASS (`test_phase20_artifact_store.py` 11/11) |
| Consented participants | **NOT AVAILABLE — BLOCKED (B4)** |

## Continuation checkpoint

1. Recruit 3–5 participants and record consent.
2. Run the pilot-plan task script per participant (45 min each).
3. Fill ONLY the tables below from observed sessions; link run IDs.
4. Submit results through the product's own feedback flow so triage
   lifecycle evidence accumulates naturally.

## Per-participant results

(filled only from real sessions)

| Participant | Tasks completed | Assistance needed | Confusing steps | Failures | Latency (perceived) | Verbatim feedback |
|---|---|---|---|---|---|---|

## Aggregate

(filled only from real sessions)

| Measure | Value |
|---|---|
| Task 1–3 unassisted completion | — |
| Takeover attempts / successes / visible stale refusals | — |
| Cross-participant isolation violations | MUST be 0 |
| New findings opened | — |
