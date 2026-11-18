# Connector Validation, Provenance & Revisions (Phase 6)

> §6.x sections map to the Phase 6 roadmap. Companions:
> [external-effects.md](external-effects.md) (Phase 5),
> [command-lifecycle.md](command-lifecycle.md) (Phase 4).

Goal (§6.0): AgentOS executes an operation only when its exact definition,
credential context, runtime, and required validation evidence satisfy the
current execution policy. Passing "list projects" never authorizes "delete
project", never certifies the whole integration, and never survives an
account switch.

## Implementation status (honest)

| Capability | Status |
|---|---|
| Provenance: unknown default, preserved through retests (§6.4) | **implemented, tested** — `validation_provenance`, `spec_source_provenance`; the test route keeps `_validation` so provenance survives |
| Canonical revision identity (§6.5) | **implemented, tested** — `connector_revisions.canonical_definition_digest`: operations sorted, keys sorted, mutable evidence excluded, format-versioned; reordering/probing cannot change identity |
| Credential bindings (§6.7) | **implemented** — `models/credential_binding.py`; binding = (secret_ref, version, epoch, account, environment); evidence binds to the CONTEXT, not the raw ref; no-auth connectors get a deterministic binding |
| Durable validation runs with stale detection (§6.8) | **implemented, tested** — `connector_validation_service`; credential/definition/runtime rotation mid-run SUPERSEDES the run; superseded evidence cannot publish |
| Operation eligibility evaluator (§6.12) | **implemented, tested** — `connector_eligibility.evaluate_operation_eligibility`; structured result with reason codes (VALIDATION_MISSING/STALE, UNKNOWN_PROVENANCE, REVIEW_REQUIRED, WRITE_VALIDATION_REQUIRED, …); enforced in `tool_policy` at the dispatch boundary and surfaced in the integrations detail API |
| Coverage-bypass fix (§6.1/§6.12) | **implemented, tested** — context-free coverage is marked `historical` and never claims current eligibility; `current` requires spec digest + binding |
| Atomic activation (§6.14) | **implemented, tested** — `connector_activation.activate_revision`: CAS on the active pointer inside BEGIN IMMEDIATE + activation event; racing activations → 1 winner + 1 conflict |
| Rollback / restore-as-copy (§6.15) | **implemented, tested** — restore creates a disabled, credential-free draft with `_validation` STRIPPED (no inherited eligibility); rollback requires CURRENT eligibility |
| Conservative migration (§6.22) | **implemented, tested** — idempotent; missing provenance → `unknown`; legacy probe results preserved as history; legacy `verified` labels are NOT converted to per-op evidence |
| Frontend operation-level truth (§6.17) | **implemented** — the integrations detail page renders the per-operation eligibility table (origin, eligibility, specific blocked reason) from the one evaluator |
| Validation profiles: static/fixture layers (§6.9) | **partially implemented** — static checks via policy+digest; fixture/live-write profiles are the documented next step, gated on §6.2 effect-path verification |
| Controlled live-write validation (§6.11) | **gated by design** — unavailable until the effect path carries validation writes (§6.2); the eligibility evaluator reports `WRITE_VALIDATION_REQUIRED` for writes |

## Key rules

1. **Provenance never upgrades by being tested** (§6.4). Categories:
   `official_spec`, `user_supplied_spec`, `extracted_docs`,
   `inferred_description`, `unknown` (default). Inferred/unknown provenance
   requires the review path before eligibility.
2. **Identity excludes evidence** (§6.5). The revision digest covers
   execution-relevant fields only — probe outcomes, timestamps, health, and
   activation flags are mutable history.
3. **Evidence binds to the validation context** (§6.7/§6.8): the
   (credential_ref, secret_version, authorization_epoch) triple. Rotation
   mid-run supersedes the run; switching accounts invalidates account-bound
   evidence. No-credential connectors bind to a deterministic no-auth
   context so attaching a credential later invalidates old evidence.
4. **One evaluator** (§6.12): policy, coverage, UI, catalog, activation, and
   self-repair all call `evaluate_operation_eligibility`.
5. **Activation is CAS** (§6.14): racing activations → 1 winner + 1 explicit
   conflict. Pinning preserves the definition, never permission —
   revocation is re-checked before every dispatch.
6. **Writes stay gated** (§6.2/§6.11): `WRITE_VALIDATION_REQUIRED` until the
   controlled-write profile runs through the Phase 5 effect path.

## Tests

`backend/tests/test_connector_phase6.py` (22 tests) — provenance survival,
identity invariance (order/probe), coverage context, validation races,
eligibility matrix, activation races, restore stripping, migration
idempotency. Existing suites (`test_connector_coverage`,
`test_execution_safety`, `test_connector_revisions`) updated to the new
contracts and passing.
