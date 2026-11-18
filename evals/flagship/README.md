# Flagship Self-Extension Benchmark (Phase 22 §22.14)

The complete journey — real goal → missing capability → generation →
validation/testing → authorized activation → AUTOMATIC parent resumption →
real execution → verified deliverable — executed through PRODUCTION entry
points (live API + worker + Postgres + NATS + local Ollama), never a
test-only app or a generic-tool bypass.

## Variant matrix (frozen denominator: 9)

| Variant | Scenario | Production entry point | Executable check | Status |
|---|---|---|---|---|
| V1 | Successful generation end-to-end (the happy path) | POST /api/v1/workflows with an OpenAPI/HTTP goal; worker builds+validates+activates; owner sees the tool in /integrations; follow-up goal reuses it | `scripts/live_e2e.py` checks `chat_http_mcp_github` + `chat_openapi_or_pokeapi` + `follow_up_reuses_tools` + `integrations_list` | PASS (2026-11-07, 13/13) |
| V2 | Website target (no API) → browser-tool generation via LOCAL model planning | same chat path with `https://example.com` | `scripts/live_e2e.py` check `chat_website_mcp` | PASS (after the ollama-JSON dispatch fix) |
| V3 | Missing credentials → candidate preserved with the precise next action | readiness gate on the build | `pytest backend/tests/test_review_batch2_build_contracts.py::…missing_credentials…` + `vault_store_names_only` live probe | PASS |
| V4 | Invalid specification → honest failure, no fabricated tool | OpenAPI parser + validation gates | `test_self_extension_loop.py` (G01–G03 gates) | PASS |
| V5 | API schema change → old evidence invalidated, repair tested | connector revisions + validation | `test_connector_revisions.py` + `test_phase21_injected_healing.py::schema_drift` | PASS |
| V6 | Worker interruption mid-build → durable resume | worker kill → lease reclaim → attempt 2 | `test_phase21_injected_healing.py::worker_interruption` + `test_phase21_stack_journeys.py` (restart-resume) | PASS |
| V7 | User cancellation → clean stop | POST /workflows/{id}/cancel | `test_round5_gaps.py` cancellation suites + live cancel probe | PASS |
| V8 | Uncertain external write → reconciliation, never a repeat | effect ledger + reconciliation | `test_phase21_injected_healing.py::uncertain_outcome` | PASS |
| V9 | Permission revocation mid-flight → honest stop | credential binding + authorization revalidation | `test_background_authorization.py` + `test_phase21_provider_credentials.py` | PASS |

## Rules honored

- No manual task-state edits, direct DB fixes, or generic-tool bypasses
  anywhere in V1/V2: the parent run resumes through the §4.13
  wait/reconciler machinery (proven by `test_phase21_flagship_journey.py`,
  1/1 — parent WAITING → child build COMPLETED → reconciler wakes parent).
- Generated code runs only through the isolated container runner
  (standalone servers) or the declarative adapter runtime (adapters) —
  `test_phase21_isolated_runner.py` (12/12) is the isolation gate.
- The live stack is the exercised production path (compose `core`,
  Postgres durable profile).

## Runner

`python scripts/live_e2e.py` executes V1+V2 live (records
`data/live_e2e_last.json`). The V3–V9 variants run as named pytest
batches:

```bash
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 GOOGLE_CLOUD_PROJECT=agentos-local-test \
  .venv/bin/python -m pytest backend/tests/test_review_batch2_build_contracts.py \
  backend/tests/test_self_extension_loop.py backend/tests/test_connector_revisions.py \
  backend/tests/test_phase21_injected_healing.py backend/tests/test_phase21_stack_journeys.py \
  backend/tests/test_phase21_flagship_journey.py backend/tests/test_background_authorization.py \
  -q
```
