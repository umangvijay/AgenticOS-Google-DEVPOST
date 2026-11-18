# Real-User Pilot Plan (Phase 22 §22.27)

Status: **PREPARED — EXECUTION PENDING REAL PARTICIPANTS** (blocker B4).
No synthetic participant may ever be labeled real-user validation; the
automated qualification used only system-level journeys (scripts/live_e2e.py,
scripts/qualify_live_probes.py) which are NOT pilot evidence.

## Preconditions (all satisfied before pilot start)

- [x] Isolation gates pass (`test_phase21_isolated_runner.py` 12/12)
- [x] Authorization/tenancy live probes pass (18/18,
      `data/phase22_live_probes.json`)
- [x] Token revocation on logout verified live (fixed + re-tested)
- [x] Feedback lifecycle gates (`test_phase21_feedback.py`,
      e2e `feedback-and-browser.spec.ts`)
- [x] Deliverable integrity gates (`test_phase20_artifact_store.py`)
- [ ] Pilot participants consented (BLOCKED — none available during the
      automated qualification window)

## Participants

3–5 real people, each on their own machine (or a supervised shared
host), using their own accounts. Consent: participants agree that
feedback and task outcomes may be recorded in
`docs/qualification/pilot-results.md` with their names REDACTED to
initials. Diagnostic sharing is opt-in per submission (the feedback
form's explicit consent flag is authoritative).

## Task script (per participant, ~45 minutes)

| # | Task | Journey |
|---|---|---|
| 1 | Sign up, connect an application (Vault credential), submit a real goal through the workspace composer | onboarding + J01 |
| 2 | Ask for a capability the product does not have (e.g. a public API the participant picks); supply app info; observe the build | J02/J03 |
| 3 | Submit a multi-step workflow (fetch → transform → deliverable) and download the deliverable | J16 |
| 4 | Open the browser panel during a browser task; pause, take over, return control | J06/J07 |
| 5 | Do ONE interrupting action mid-run (refresh the page, kill the worker via the operator, or cancel) and recover | J09/J11 |
| 6 | Submit feedback from a completed AND a failed run | J17 |

Recorded per task: completion (independent of participant's
impression — verified against API state), assistance needed, confusing
steps, failures, latency (participant-perceived), cost shown in the UI,
verbatim feedback.

## Success criteria (pre-declared)

- ≥ 70% of participants complete tasks 1–3 unassisted.
- Task 4: every participant who attempts takeover succeeds while the
  agent's stale actions are refused (they must SEE the refusal).
- No participant can access another participant's data (spot-checked
  by the operator via the API).
- All failures observed by participants already have matching
  fault-matrix entries or become new findings.

## Outputs

`docs/qualification/pilot-results.md` — filled ONLY from actual
sessions; explicitly marked PENDING until then. Raw feedback stays in
the product's feedback store (triage states apply); the results file
links run IDs, never raw credentials.
