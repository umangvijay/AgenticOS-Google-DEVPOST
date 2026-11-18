# Fault Matrix (Phase 22 §22.19 + §22.10)

Predeclared fault classes, the injection point, the required recovery
behavior, and the executable check. Recovery may never broaden
permissions, erase uncertainty, or count "repair scheduled" as "repair
done". Supported failures must recover within declared bounds;
unsupported failures must stop honestly.

| # | Fault | Injection point | Required result | Executable check | Status |
|---|---|---|---|---|---|
| F1 | Model transport failure (connection reset) | agent/model client | classified transient; bounded retries (ceiling 3); honest stop after ceiling | `test_phase21_injected_healing.py::connection_reset` | pending-reverify |
| F2 | Provider rate limit (429) | metered dispatch | ceiling 2 → escalate (never hammer) | `test_phase21_injected_healing.py::rate_limit` | pending-reverify |
| F3 | API schema change (422) | tool validation | identical resend forbidden; same repair twice refused | `test_phase21_injected_healing.py::schema_drift` | pending-reverify |
| F4 | Worker loss mid-task (lease held) | worker process kill | lease reclaim → attempt 2 re-executes from durable state | `test_phase21_injected_healing.py::worker_interrupt` (harness fault stage 35/35) + LIVE: api restart mid-run → run completed through the durable path (run 03e65266) | verified |
| F5 | Uncertain external write | effect dispatch | dispatch NOT repeated; reconciliation resolves from provider evidence | `test_phase21_injected_healing.py::uncertain_outcome` + effect suites | pending-reverify |
| F6 | Command persisted, publish lost | outbox dispatcher | reconciliation eventually dispatches (no silent loss) | outbox recovery suites + stack journey | pending-reverify |
| F7 | Duplicate delivery after publish-before-ack | message bus consumer | at-least-once handled without duplicate consequential effects | idempotency repository suites + effect dedup | pending-reverify |
| F8 | Expired/stale lease owner | capacity + task repos | ownership fenced; stale writes refused | `test_phase21_browser_takeover.py::stale_epoch` + lease suites | pending-reverify |
| F9 | Missing credentials between plan and dispatch | tool router | candidate preserved; precise next action; no rebuild | `test_review_batch2_build_contracts.py::missing_credentials…` | pending-reverify |
| F10 | Stale browser target (DOM changed) | browser runtime | re-resolution before acting; verified postcondition | browser runtime suites (staleness) | pending-reverify |
| F11 | Interrupted artifact publication | publisher | publication job retried; completion event emitted | deliverable/publication suites | pending-reverify |
| F12 | Poisoned state (no repair possible) | recovery RL env + runtime | honest escalate; budget burn shaped against | `test_phase21_rl_real_tasks.py::critical` | pending-reverify |
| F13 | OOM / fork bomb / output flood in generated code | container runner | killed by caps; host untouched; descendants die | `test_phase21_isolated_runner.py` (10 hostile fixtures) | pending-reverify |
| F14 | Non-cooperative cancel (ignores SIGTERM) | container runner | SIGKILL within deadline; wall time bounded | `test_phase21_isolated_runner.py::cancel` | pending-reverify |
| F15 | Storage outage / disk-full (postgres crash-loop) | repositories/startup | loud failure (no silent degradation), recovery on reconnect | LIVE: VM disk filled during qualification (2026-11-07 18:09) — postgres PANIC `No space left on device`, crash-looped loudly, WAL-recovered automatically to healthy after space was freed; the interrupted run (03e65266) then COMPLETED through restart-resume; `test_phase21_platform_integration.py` + compose journey | verified (live) |
| F16 | Repeated recovery failure | recovery worker | escalation to honest stop, episode records attempts/cost | recovery suites + F1–F3 ceilings | pending-reverify |
| F17 | Cleanup job failure | memory cleanup worker | durable retry with attempts + error visible | `test_phase21_memory_dependencies.py::cleanup_retry` | pending-reverify |
| F18 | Provider outage (no response) | inference dispatch | explicit unavailable — never canned output | `test_no_canned_outputs.py` + `test_phase21_dynamic_behavior.py` | pending-reverify |

Denominator note: 18 fault classes; each maps to exactly one primary
executable check (plus supporting suites). Recovery-verified-rate
denominator (release-scope.md ≥60%): escalated recovery episodes with a
terminal state, numerator = VERIFIED_RECOVERY episodes, measured from
recovery episode records — never from action identity.
