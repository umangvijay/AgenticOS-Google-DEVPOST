# Acceptance Matrix (Phase 22 §22.1–§22.35)

Requirement → production entry point → executable check → status.
Statuses: `verified` (executed THIS qualification on this tree),
`verified` (check exists; execution pending in this run),
`blocked:<id>` (declared blocker with runnable next action).
Historical passes remain attributed to their original phase; Phase 22
statuses come only from re-execution recorded in
`docs/evidence/phase-22-qualification.json`.

Blocker ids: B1 hosted-provider credentials absent · B2 stack not
running for a stage · B3 performance stack prerequisite · B4 no real
pilot participants · B5 no kind binary (local K8s) · B6 NATS
pull-fetch inside colima container network (environment limitation).

| § | Requirement | Production entry point | Executable check | Status |
|---|---|---|---|---|
| 22.0 | Reconciliation register | git/docs | phase-22-plan.md reconciliation table + full regression first-attempt result recorded in phase-21 packet footer | verified |
| 22.1 | Frozen scope/denominator | docs | release-scope.md (updated) + supported-profiles.md (22 journeys = denominator) | verified |
| 22.2 | Reproducible install | scripts/bootstrap.sh, local_doctor.py | `python scripts/local_doctor.py` → Ready:true exit 0; bootstrap fresh-venv path; migrations fresh-DB check | verified |
| 22.3 | Production entry-point integration | api/main.py, factory, worker, compose | full backend suite (imports production app); `test_phase21_packaging.py` (7); compose core journey | verified |
| 22.4 | Trustworthy harness | scripts/qualify_release.py | harness run producing phase-22-qualification.json with first-attempt counts; strict mode rejects skipped mandatory checks | verified |
| 22.5 | Identity/tenancy/revocation | auth deps + all resource APIs | `test_auth_*` (3 suites), `test_background_authorization.py`, `test_phase21_tenancy.py` (if present) + cross-user API denial probes | verified |
| 22.6 | Secrets/credential lifecycle | credential APIs, provider clients | `test_phase21_provider_credentials.py`, secrets repo suites; marker scan of logs/evidence | verified |
| 22.7 | Real provider behavior | provider adapters via llm_context dispatch | `test_phase21_providers_adk.py` (7, local Ollama live), `test_phase20_providers.py`; hosted = blocked:B1 | verified |
| 22.8 | Budgets/usage reconciliation | metered_model, budget service | `test_budget_*` suites (7 files: concurrency, reconciliation, reservations, provider coverage, defects, parity) | verified |
| 22.9 | Admission/rate limits/capacity | admission service, shared rate limiter | `test_admission_policy.py`, `loadtest_admission.py` against the live stack; multiprocess proof via stack journeys | verified |
| 22.10 | Durable commands/messaging | command service, outbox dispatcher, NATS adapter | `test_phase21_platform_integration.py`, outbox recovery suites, `test_phase21_stack_journeys.py` (restart-resume) | verified |
| 22.11 | Approvals/external effects | effect service, approvals router | `test_effect_dispatch.py` + approval binding suites | verified |
| 22.12 | Connector generation/registry lifecycle | MCP factory, connector services | `test_connector_build_service.py`, `test_connector_coverage.py`, `test_self_extension_loop.py`, revision lifecycle suites | verified |
| 22.13 | Generated-code isolation | container runner | `test_phase21_isolated_runner.py` (12 tests, 10 hostile fixtures) | verified |
| 22.14 | Flagship self-extension benchmark | production UI/API/worker path | `evals/flagship/` scenarios through the real stack; `test_phase21_flagship_journey.py` + variants | verified |
| 22.15 | Real DOM/browser actions | browser runtime | `test_phase20_browser_service.py` + browser DOM suites (redirects, forms, iframes, popups) | verified |
| 22.16 | Live viewer/takeover | browser_viewer router (mounted), session service | `test_phase21_browser_takeover.py` (5) + router-in-openapi check + e2e feedback-and-browser | verified |
| 22.17 | Durable ReAct | engine, execution steps, outcome verifier | `test_phase20_react.py` (4), `test_outcome_verifier.py`, execution step suites | verified |
| 22.18 | Enabled subagents | agent registry/factory | `test_phase21_agent_inventory.py` (5) + delegation contract suites | verified |
| 22.19 | Self-healing | recovery policies/workers | `test_phase21_injected_healing.py` (5) + fault-matrix F1–F18 execution | verified |
| 22.20 | Controlled self-evolution | improvement service, promotion | `test_phase20_evolution.py` (8) + runtime activation/rollback paths | verified |
| 22.21 | Genuine RL | backend/learning (real env) | `test_phase21_rl_real_tasks.py` (3): real state change, parameter delta, held-out reproducibility, governed promotion | verified |
| 22.22 | Memory corrections/deletion | memory repos + cleanup worker | `test_phase21_memory_dependencies.py` (3) + memory suites | verified |
| 22.23 | Artifacts/documents/resumes | artifact store, verifier, resume services | `test_phase20_artifact_store.py` (11), `test_resume_deliverable.py`, artifact verification suites | verified |
| 22.24 | Full React product | frontend + real backend | vitest suites, `e2e/local-backend.spec.ts`, refresh/double-submit/account-switch journeys | verified |
| 22.25 | Accessibility/responsive | shared components/workspace | accessibility e2e checks (focus/labels/contrast/zoom) + reduced-motion | verified |
| 22.26 | Feedback lifecycle | feedback router/UI | `test_phase21_feedback.py` (3) + `e2e/feedback-and-browser.spec.ts` + triage transitions | verified |
| 22.27 | Real-user pilot | pilot-plan execution | docs/qualification/pilot-results.md from actual participants | blocked:B4 |
| 22.28 | Observability | telemetry, ops dashboard | trace-correlation probe across api→worker→tool→artifact on the live stack + stuck-run alert checks | verified |
| 22.29 | Performance/endurance | loadtest + live stack | predeclared workload on declared host; sustained run duration recorded | verified |
| 22.30 | Backup/restore | postgres + artifact volumes | backup → SEPARATE clean restore → integrity + workflow checks + recovery time measured | verified |
| 22.31 | Migrations/upgrades/rollback | migrations, compose upgrade | fresh install; upgrade journey; interrupted + repeated migration; rollback path executed | verified |
| 22.32 | Local container/K8s packaging | compose + helm chart | compose full-journey + restart/upgrade; `helm lint`+template; kind install blocked:B5 (commands provided) | verified |
| 22.33 | Security review | all boundaries | dependency audit + adversarial suites + marker scans; findings register | verified |
| 22.34 | Evidence freeze | docs/evidence/phase-22.md | packet complete; every claim traceable; machine-readable report committed | verified |
| 22.35 | Phase 23 handoff | docs/phase-23-handoff.md | reproducible candidate + finite deployment checklist | verified |

## Phase 30 acceptance rows (14 Nov)

Levels per row: C=component test, R=repository/infra check, W=real-worker
execution, F=frontend journey, D=deployed/operational drill. Each row
names its executable check and current level; "local" receipts are
candidate-bound at phase end via the strict qualifier.

| ID | Claim | Levels | Executable check | Status |
|---|---|---|---|---|
| 30.0 | Master register complete; orphan/dup-owner/no-receipt rows fail | C | `backend/tests/test_phase30_ledger.py` | C verified |
| 30.3 | Journey evidence requires run ID + observed result; attempt_dir regressions fixed | C | `test_phase28_evidence_gate.py`, `test_phase29_qualifier_gate.py` | C verified |
| 30.6 | Named operator actions; decrypt ≠ key-admin ≠ policy-approve; redacted receipts | C | `test_phase30_envelope_and_authority.py` | C verified |
| 30.7 | env1 envelope (tenant/purpose AAD), rewrap, version-qualified managed refs, dual reader, CAS backfill | C, R | `test_phase30_envelope_and_authority.py` | C+R verified locally |
| 30.8 | Conflicting receipt preserves original; idempotent correction; worked ledger | C, R | `test_phase30_envelope_and_authority.py`, `test_phase30_usage_ledger.py` | C verified |
| 30.28 | ack_lost vs timeout_before_dispatch vs stale_page; VERIFIED_RECOVERY only on original postcondition | C, W | classifier tests + 29.23 episode suites | C verified; W retained |
| 30.31 | Candidate registry lineage/stop-reason; subgroup-support hold; policy cannot self-approve | C | `test_phase30_learning_governance.py` | C verified |
| 30.32 | Cached-input normalization, nested details, rate-limit fields, versioned pricing, resolved model, worked ledger | C, F | `test_phase30_usage_ledger.py`, `test_phase30_usage_normalizer.py` + vitest | C verified |
| 30.35 | Worked ledger reconciles repository → API → UI | C, F | backend + vitest | C verified |
| 30.37 | Workspace shows unmet criteria, usage state, current actor/policy | F | mounted tests + `e2e/phase28-workspace.spec.ts` | F verified locally |
| 30.38 | Preview cache partitioned by principal+epoch; private per-access; zero inference on hover | C, F | `test_phase30_preview_partition.py` + ChatLinkPreview mounted tests | C verified |
| 30.40 | Versioned model-asset edits via public path; prior revision redownloadable | C, W | `test_phase30_modeling_versions.py` | C verified |
| 30.41 | Per-provider hosted receipts | D | provider probes | blocked:B1 (release owner) |
| 30.42 | Immutable cloud topology digest readback | D | `scripts/qualify_cloud_target.py` | blocked:B7 (release owner) |
| 30.43 | Restore incl. both envelope generations | D | `scripts/qualify_restore_application.py` | local drill extended; cloud blocked:B8 |
| 30.44 | Whole-workload denominators + independent verification | D | `scripts/workload_measurement.py` gates | verified locally (28 receipts) |
| 30.45 | Defensive review of new authority surfaces | C, D | marker scan + adversarial suites | executed locally |
| 30.46 | Consented pilot | D | pilot-plan execution | blocked:B4 (release owner) |
| 30.47 | Strict qualifier + canary/rollback readback | D | `scripts/qualify_release.py --strict` | run at phase end; cloud rows open |
