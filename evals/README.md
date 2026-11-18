# Evaluation Data & Outcome Verification (Phase 12)

This directory documents AgentOS's evaluation contract: how run outcomes
are verified, what evidence backs each score, and how to reproduce
results. The implementation lives in:

| Piece | Location |
|---|---|
| Scorer (evidence contract) | `backend/services/evaluation_runner.py` |
| Adapter (worker path) | `backend/services/evaluation_service.py` (`evaluate_run_async`) |
| Durable records | `backend/repositories/evaluation_repository.py` (+ `firestore/evaluation_repository.py`) |
| API | `backend/api/routers/evaluations.py` (`/api/v1/evaluations/...`) |
| Run UI | `frontend/src/components/EvaluationPanel.tsx` |

## The evidence contract

An evaluation record is **never a placeholder**. Every score is derived
from durable evidence written during execution:

1. **Task completion** — task states from the workflow repository.
   Overall success requires ALL tasks `COMPLETED`.
2. **Acceptance criteria (Phase 10 §10.7/§10.8)** — each task's outcome is
   verified with the `OutcomeVerifier` against its acceptance criteria.
   A task with no criteria is `needs_review`, never silently passed:
   "the output is non-empty" is not completion evidence.
3. **Effect ledger (Phase 5)** — external actions must be
   `CONFIRMED_APPLIED`. `OUTCOME_UNKNOWN` / `DISPATCHING` /
   `PROVIDER_PENDING` effects mean the run is NOT verified (uncertainty
   is honest — interaction ≠ acknowledgment ≠ business verified).
   `CONFIRMED_NOT_APPLIED` fails the run.
4. **Execution steps (Phase 10 §10.5)** — tool-call counts and error-code
   accuracy rates (unknown-tool / invalid-argument / schema-violation)
   come from the durable `execution_steps` table, and latency is
   observed from step timestamps.
5. **Token spend** — the per-run counter drained exactly once at
   evaluation time (`drain_run_tokens`), so the record carries the REAL
   spend.

## Record identity & immutability semantics

- Every record carries `evaluator_version` (currently `2.0.0`) and a
  `fixture_hash` — the SHA-256 over the evaluated task ids + criteria.
- **Fixed fixtures reproduce identical results**: the same task set and
  criteria always produce the same `fixture_hash` and the same verdicts.
  Changing the fixture (tasks or criteria) changes the hash — so a
  silently different eval can be detected.
- **Scorer changes cannot rewrite history silently**: records are
  append-only identity-keyed rows (`evaluation_records.evaluation_id`
  PRIMARY KEY); each stores the scorer version that produced it. A new
  scorer version writes NEW records; comparisons across versions are
  explicit, never in-place mutations.
- **Records survive restart**: they live in the `evaluation_records`
  table (SQLite) or the `evaluation_records` collection (Firestore),
  written once per terminal run by the worker's run-finish path.

## Overall success rule

```
success = (all tasks COMPLETED)
          AND no effects in {DISPATCHING, PROVIDER_PENDING, OUTCOME_UNKNOWN}
          AND no effects in {CONFIRMED_NOT_APPLIED}
          AND at least one task
```

A FAILED task always fails the run, even with confirmed effects.

## Running / verifying

```bash
# Unit + acceptance tests (restart survival, fixture reproduction,
# effect semantics, tool metrics, API authorization):
.venv/bin/python -m pytest backend/tests/test_evaluation_phase12.py \
    backend/tests/test_evaluation_evidence_integration.py \
    backend/tests/test_evaluations_api.py -q

# Legacy facade contract (real per-run token spend):
.venv/bin/python -m pytest backend/tests/test_self_healing_and_learning.py -q
```

## API surface

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/v1/evaluations/runs/{run_id}` | owner or admin | All records for a run |
| `GET /api/v1/evaluations/{evaluation_id}` | owner or admin | One record (public projection) |
| `GET /api/v1/evaluations/summary` | admin only | Aggregate: success rate, latency, tokens, recoveries |

Tenant scoping is enforced from the record's `user_id`; cross-tenant
reads are 403, missing repos are 503 (fail-closed).
