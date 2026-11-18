# Supported Profiles (Phase 22 §22.1, revised Phase 32 §32.1)

> Historical configuration inventory. Current production qualification is
> incomplete; see [Phase 23](phase-23-plan.md) and
> [phase-32-plan.md](phase-32-plan.md). Packaging support is distinct
> from executed qualification on an OS, architecture, or deployment target.
>
> **Phase 32 §32.1 revision (2026-11-14):** the journey denominator is
> J01–J24 across 18 stages (supersedes the frozen "J01–J22" line below);
> the measurement denominator and capability controls are frozen in
> [release-scope.md](../release-scope.md) (Phase 32 revision). Cloud
> deployment/inference provider combinations are declared per target in
> [phase-32-target-matrix.md](phase-32-target-matrix.md); unassigned
> values (cloud RTO/RPO, canary cohorts, cost envelopes) stay visibly
> pending and hold their dependent decisions.

This is the frozen supported-configuration matrix for the Phase 22
release candidate. Anything outside this matrix is unsupported for the
release claim (it may work, but is not qualified).

## Supported local operating profiles

| Profile | OS/Arch | Verified on |
|---|---|---|
| `local-core` | macOS arm64 (darwin 27) via colima VM, Docker 29.x | THIS host, 2026-11-07 |
| `local-core` Linux x64/aarch64 (container-native) | packaged, not target-qualified | no execution on a second host recorded here |
| Windows native | NOT supported in this release | — |

## Process profiles

| Profile | Components | Transport | Storage |
|---|---|---|---|
| Single-process dev | API + in-process worker | in-memory bus | SQLite |
| Durable local (`compose --profile core`) | api, worker, nats, postgres, frontend | NATS JetStream + Postgres transactional outbox | PostgreSQL 16 |
| Emulator cloud parity | firestore emulator | in-memory | Firestore (emulator) |
| Browser profile (`--profile browser`) | browser-worker (Chromium) | NATS | Postgres |
| Runner profile (`--profile runner`) | isolated container runner | docker socket (scoped) | Postgres |
| Local AI (`--profile local-ai`) | ollama (qwen2.5:3b-instruct) | HTTP :11434 | n/a |

## Supported databases

- PostgreSQL 16 (durable local + cloud path) — contract suites +
  migrations 0001–0013.
- SQLite (single-host dev) — full contract-parity suites.
- Firestore (cloud profile; verified against the local emulator — real
  Firestore project qualification is a Phase 23 target).

## Model providers (application-accepted)

| Provider | Mode | Phase 22 evidence |
|---|---|---|
| Ollama (local) | live local inference (qwen2.5:3b-instruct) | EXECUTED (real inference, tool calls, metering) |
| Vertex / Gemini | hosted — credential-gated | adapters + readiness ladder proven; live call BLOCKED (no key on host) |
| Bedrock (Converse) | hosted — credential-gated | same as Vertex |
| Groq | hosted — credential-gated | same |
| HuggingFace | hosted — credential-gated | same |

Rules: missing credentials → explicit refusal (never a canned answer);
per-user credential binding is request-scoped; provider readiness
ladder `configured → credentials_accepted → model_accessible →
inference_verified` computed against the acting user's credential.

## Supported browsers

- Chromium-family (Playwright Chromium build) for agent sessions and
  product e2e. Firefox/WebKit: not qualified this release.

## Supported tool types

1. Declarative REST adapters (OpenAPI-spec / NL sources) through the
   validation pipeline (G01–G03 gates).
2. Generated standalone MCP servers executed ONLY through the isolated
   container runner (sibling-container profile; network default-deny).
3. Built-in agent tools (browser actions, email, artifacts) through the
   effect/approval boundary.

## Deliverable formats

- Markdown/text/JSON artifacts + resume/document exports (PDF/DOCX
  via the artifact service), checksummed per-file, revisioned,
  ownership-checked, authorized-download only.

## Resource requirements (single host)

- ≥ 4 CPU cores, ≥ 8 GB RAM for the durable profile (api+worker+pg+nats
  +ollama ~2 GB model); colima VM provisioned accordingly.
- Disk: ≥ 10 GB (images + model + artifacts).
- Host ports: 8000 (api), 3000 (frontend), 5432 (postgres), 4222 (nats),
  11434 (ollama), 8080 (firestore emulator).

## Explicit limitations (declared, not hidden)

1. Hosted provider live-inference evidence requires user-supplied keys
   (BLOCKED on this host; runnable commands recorded in the packet).
2. NATS JetStream pull-fetch inside the colima container network hangs
   in this environment (colima userland networking); host-side NATS is
   proven and the durable delivery path in containers is the Postgres
   transactional outbox. Recorded as an environment limitation.
3. Local Kubernetes (kind) install/upgrade evidence requires the kind
   binary (not on host at qualification start; command provided).
4. Real-user pilot requires actual participants (none available during
   the automated qualification window → pilot plan prepared, execution
   pending — never fabricated).
5. Laptop/VM performance numbers do not generalize to arbitrary cloud
   capacity (Phase 23 measures on target infrastructure).
6. 3D scene is decorative; every mandatory journey works with it
   disabled.

## Mandatory journey denominator (frozen)

J01–J22 as listed in the Phase 22 specification — 22 journeys, each
requiring its own acceptance case. "100% complete" requires passing,
candidate-matched evidence for every mandatory journey. A declared runnable
blocker remains incomplete and cannot be counted as a pass. Optional enterprise
expansion (SSO/SCIM, marketplace publishing, billing) is OUT of the
release denominator.
