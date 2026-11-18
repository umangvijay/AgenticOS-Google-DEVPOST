# The learning pipeline (§20.21–§20.23)

## Classified self-healing (§20.21)

Failures are classified FIRST (`backend/engine/failure_classifier.py`:
timeout, transient_network, server_error, rate_limited,
schema_mismatch, auth_invalid, not_found, auth_expired, internal), then
the deterministic gate (`backend/services/recovery_policy.py`) decides
from the attempt LEDGER: category ceilings (timeout 3, server_error 2,
schema_mismatch 2 via argument REPAIR — an identical-call resend is
never the recovery), repeated-action refusal (the same
(tool, category, args) twice proves the loop ineffective → escalate),
absolute 15-minute deadline, and immediate escalation for
ceiling-0 categories (a rejected credential needs a human).

Evidence: `pytest backend/tests/test_phase20_healing.py -q` (15 tests
— every class injected).

## Genuine RL training (§20.23)

`backend/learning/` trains a REAL PPO policy
(stable-baselines3) on `RecoveryEnv` — a gymnasium environment whose
every step executes REAL subprocesses and rewards from independently
checked exit codes (no simulated execution):

```bash
pytest backend/tests/test_phase20_rl.py -q     # ~30 s: params change, checkpoint loads
.venv/bin/python -m backend.learning.train_recovery_policy --timesteps 50000
# → data/rl/recovery_policy.zip + training-record.json (fresh rollouts,
#   held-out eval across seeds, promote-or-keep decision vs baseline)
```

The promotion decision is honest by construction: a candidate under
`PROMOTION_THRESHOLD` (0.75 held-out accuracy) loses to the heuristic
baseline and the run records that outcome — training does not
manufacture a win.

## Controlled self-evolution (§20.22)

`backend/services/improvement_service.py` gates every candidate change:
propose (pinned immutable manifest + dataset hash) → evaluate (baseline
AND candidate on the SAME held-out set; a swapped dataset is refused) →
shadow → bounded canary (cohort fraction predeclared) → promote
(deterministic gates: human non-proposer approver, typed evidence,
zero critical-constraint violations, counted canary exposures) — or
reject (explicit, recorded) — or rollback (real restore hook runs
exactly once; promoted-only).

Agents can propose bounded improvements but can NEVER evaluate, grade,
approve, promote, or roll back. Evidence:
`pytest backend/tests/test_phase20_evolution.py -q` (8 tests).
