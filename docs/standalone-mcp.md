# Standalone MCP Servers & Isolated Execution (Phase 7)

> §7.x sections map to the Phase 7 roadmap. Companions:
> [connector-validation.md](connector-validation.md) (Phase 6),
> [external-effects.md](external-effects.md) (Phase 5).

Goal (§7.0): AgentOS can package a standalone MCP server, test its actual
behavior inside an isolated runtime, and execute the exact tested artifact
under controlled permissions.

## Three product capabilities (§7.2) — separate gates

| Capability | Status |
|---|---|
| Declarative adapter | **Fully implemented** (Phases 3/6: op-level eligibility, policy gate, shared runtime) |
| Portable source export | **Implemented** — download is available BEFORE runtime validation and is clearly marked **untested**; export never activates anything |
| Managed standalone execution | **Local container implementation available; cloud qualification pending** — the runner contract, profiles, attestations, and deployment eligibility are implemented and tested; the default runner returns the explicit "unavailable" result until a backend (Cloud Run sandbox / gVisor / microVM, §7.5) is proven against the isolation acceptance profile |

## Phase 26 managed build continuation

See [Managed MCP builds](managed-mcp-builds.md) for the executable `python_tools`
build API, generated stdio artifact, host-observed protocol validation, signed
activation, durable parent resumption and brokered anonymous HTTPS reads. The
configured runner can use a shared PostgreSQL execution journal. The older tests
that seed attestation fixtures remain gate-contract tests; the new Phase 26
journeys generate their attestations from real exchanges. Cloud-profile isolation
and deployment qualification remain mandatory.

## What is implemented and tested

- **Artifact model (§7.7)**: `models/mcp_artifact.py` — content identity
  (source bundle, tool schemas, dependency lock, entrypoint, transport)
  with EXCLUDED evidence/flags; unsafe source paths rejected; INDEPENDENT
  status fields (export_status, build_status, protocol_validation_status,
  isolation_validation_status, deployment_status) — `deployable` is not the
  authority.
- **Trusted attestations (§7.19)**: HMAC-signed by the control-plane issuer
  (the key never reaches generated code); bound to the exact artifact +
  runtime + suite digests; forged documents rejected; one artifact's
  evidence cannot authorize another; retesting appends history.
- **Deployment service (§7.20)**: evaluates from CURRENT records — content
  integrity (post-test change blocks), trusted attestation coverage,
  isolation profile, op-level Phase 6 eligibility, revocation, budget.
  `test image A → rebuild → deploy B` is structurally prevented.
- **Runner contract + profiles (§7.11/§7.12)**: `services/isolated_runner.py`
  — submit/get/cancel/terminate/collect/cleanup; runtime profiles with
  supervisor-enforced deadlines; the write_connector profile is GATED on the
  verified Phase 5 effect boundary (§6.2/§7.3); execution state separate
  from cleanup state.
- **Candidate diagnostics (§7.10)**: `analyze_candidate` returns structured
  findings (syntax, unsupported imports, forbidden capability requests,
  embedded credentials, dynamic installation, entrypoint); a clean result is
  diagnostic-only and never substitutes for isolation.
- **MCP client result preservation (§7.17)**: `call_tool` returns the
  COMPLETE result — content blocks, structured output, and the tool's own
  error status (`is_error`) — so a provider tool error can no longer become
  an ordinary successful result; tool listing follows full pagination.

- **Isolation decision record (§7.5)**: **implemented** — the 10 executable
  acceptance tests (`ISOLATION_ACCEPTANCE_TESTS`), the runtime decision
  record (Cloud Run sandbox `selected_pending_proof`, gVisor fallback), and
  the documented filesystem constraint (minimal runner image REQUIRED);
  managed execution stays unavailable until proven live.
- **Unified artifact service (§7.8)**: **implemented, tested** —
  `artifact_service.package_artifact` produces the immutable artifact + the
  portable ZIP FROM THE SAME BYTES (nested structures deep-copied at the
  boundary; deterministic sorted entries; normalized metadata); exports
  ship marked UNTESTED with the honest README.
- **Dependency lock (§7.9)**: **implemented, tested** — exact distributions
  with integrity hashes for direct AND transitive deps; hash-verification
  rejects changed packages and silently-added dependencies; the write path
  (isolated builder → verify → lock → image) is documented for deployment.
- **Operation broker (§7.14/§7.15)**: **implemented, tested** —
  `evaluate_broker_request` verifies tenant/execution/artifact identity,
  method and destination allowlists, capability expiry and REPLAY, and
  request budget; capabilities replace reusable provider secrets.
- **Protocol validation service (§7.16)**: **implemented, tested** — the
  required-case suite (initialize, listing, valid/invalid calls, unknown
  tool, error preservation, shutdown), suite digest (suite changes
  invalidate older attestations), skipped-required ≠ passed, uncertain ≠
  pass.
- **Self-repair (§7.22)**: **implemented, tested** — failure
  classification → justified-regeneration decision (credential failures do
  NOT regenerate) → immutable candidate → ordinary gates; the proposer
  cannot self-approve, weaken the suite, or exceed attempt/cost limits.
- **Operational controls (§7.24)**: **implemented, tested** — kill switches
  (disable new executions, revoke artifact/image, drain runner version,
  stop provider access without deleting evidence), tenant-wide
  cancellation, orphan reconciliation; telemetry counters with bounded
  labels.

## Documented pending (§7.26 order)

- **Runner backend proof (§7.5)**: run the 10 acceptance tests against
  Cloud Run sandbox in a live deployment; managed execution opens only
  after all pass.
- **Live dependency build pipeline execution (§7.9 steps 5-9)**: the lock
  schema/verification are implemented; the isolated builder run, SBOM
  generation, and image build are deployment-side steps.
- **Adversarial isolation matrix (§7.23)**: host-secret reads, metadata
  access, memory exhaustion — execute against the proven backend.
- **HTTP transport protocol tests (§7.16)**: stdio is the tested transport;
  HTTP deployment is NOT advertised (§7.25).

## Tests

`backend/tests/test_standalone_phase7.py` (14 tests): attestation
forgery/coverage, deployment eligibility matrix (content-change block,
isolation bypass, revocation, foreign caller, blocked ops), runner gating,
profile limits, candidate diagnostics, artifact identity invariance.
