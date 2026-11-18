# Release Scope (Phase 00 §00.1)

**This document defines what "working" means for the next release
candidate.** A feature is IN SCOPE only when it has: a concrete fixture,
an expected outcome, executed checks, and recorded evidence. Component
existence is never proof of readiness (§00 acceptance).

The live requirement-and-evidence ledger with per-item statuses lives at
[REQUIREMENT_LEDGER.md](REQUIREMENT_LEDGER.md); the executed-check
history lives in [RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md).

## Supported journeys (in scope)

| # | Journey | Fixture / check | Expected outcome |
|---|---|---|---|
| 1 | Authenticated API read via an existing connector | tool-router + effect suites (`test_effect_dispatch.py`) | task verified; effect CONFIRMED_APPLIED |
| 2 | Missing connector built from supplied API info | `test_connector_build_service.py`, `test_review_batch2_build_contracts.py` | durable command → build → validated operations → activated revision |
| 3 | Authentication required after generation | readiness gate (`test_review_batch2_build_contracts.py::test_missing_credentials_preserve_candidate_with_next_action`) | candidate preserved; precise next action; no rebuild |
| 4 | Parent resumes after required-operation validation | §12.7 readiness ladder | resume ONLY at ACTIVATED |
| 5 | Temporary failure recovered | `test_recovery_worker_acceptance.py` | one logical recovery (episode claim authority), verified before success |
| 6 | Uncertain write pauses for reconciliation | effect suites (§5) | OUTCOME_UNKNOWN blocks verified success until evidence resolves |
| 7 | Real artifact delivered and checked | verifier artifact criterion with resolver | unresolved artifact can never pass |
| 8 | Honest failure reporting | `test_review_batch1_outcomes.py` (needs_review parks the run), `test_phase13_rollout.py` (constraint veto) | unverified outcomes never render as success |

## Explicitly EXCLUDED from the release claim

- SSO / SCIM provisioning, billing (§21.7-8) — later product phases;
  settings screens are not capability.
- Third-party marketplace **publishing** (submitting apps to a public
  marketplace) — later product phase. Reconciled with Phase 27/28: the
  declared-app install → credential binding → qualified-operation
  lifecycle is now IN SCOPE as journey J23
  ([qualification/phase-27-plan.md](qualification/phase-27-plan.md)).
- Cloud deployment qualification (AWS/GCP/Azure) — separate per-provider
  decisions (Phase 28 batch 28.10). The local `local-durable-postgres`
  target has its own executed stage; a local pass never qualifies a
  cloud target.

Previously-excluded items closed by later phases (self-extension
benchmark, real-browser journeys, same-session takeover, arbitrary
generated-code isolation) are now IN SCOPE with executed evidence —
see [qualification/phase-22-plan.md](qualification/phase-22-plan.md)
and the Phase 22 evidence packet.

## Supported technical scope (Phase 22 frozen matrix)

The complete supported-configuration matrix lives at
[qualification/supported-profiles.md](qualification/supported-profiles.md).
Summary:

- **Storage**: PostgreSQL 16 (durable local + cloud path), SQLite
  (single-host dev), Firestore (cloud profile, emulator-verified) — all
  selected through `RepositoryFactory` with contract-parity suites.
- **Messaging**: NATS JetStream + Postgres transactional outbox
  (durable profile); in-memory bus (single-process dev).
- **Model providers**: Ollama local (live-verified); Vertex/Bedrock
  adapters through ordinary execution (hosted live calls
  credential-gated); Groq/HuggingFace adapters (credential-gated).
- **Isolation**: generated code executes ONLY through the isolated
  container runner (sibling containers, network default-deny,
  resource-capped) with the ten isolation acceptance tests.
- **API patterns**: REST via the MCP/adapter pipeline; OpenAPI-spec and
  natural-language sources for builds.
- **Authentication**: API keys and OAuth credentials bound by reference;
  per-origin injection with challenge gating.
- **Artifact formats**: resume/document exports and file artifacts via
  the artifact service (checksummed, ownership-checked).
- **Browsers**: Chromium-based sessions with semantic DOM targeting,
  staleness re-resolution, ownership fencing, live viewer + takeover.

## Product-wide quality measures (§00.5) and initial thresholds

| Measure | Source | Initial threshold |
|---|---|---|
| Verified task completion | evaluation records (`success`) | ≥ 85% on the held-out suite |
| Unauthorized-action count | audit log + admission refusals | 0 (hard) |
| Duplicate external effects | effect ledger digest dedup | 0 (hard) |
| Recovery verified rate | recovery episodes (VERIFIED_RECOVERY / terminal) | ≥ 60% of escalated episodes avoided |
| Cost per completed task | usage ledger `usage_summary_for_run` | tracked; regression > 2× blocks promotion |
| Abandoned workflows | runs stuck non-terminal > 24h | < 1% of runs |

Thresholds were set BEFORE measuring candidate releases (§00.5) and
change only through a reviewed, versioned change.

## Reproducible verification (§00.4)

`scripts/verify-local.sh` runs the full local gate: dependency
resolution, migrations against a fresh database, the backend suite, the
Firestore parity suite when an emulator is available, and the frontend
type-check. Results are recorded in RELEASE_EVIDENCE.md per candidate
with the commit and working-tree identity.

## Phase 32 §32.1 revision — frozen measurement denominator and release controls (2026-11-14)

This revision freezes the values the Phase 32 measurement uses. It is
versioned: the §00.5 thresholds above are unchanged; the additions below
are owned by Phase 32.

**Mandatory journey denominator:** J01–J24 (24 journeys; J23
marketplace, J24 modeling; J14 bound to `rl_training`) across 18
mandatory stages — per `scripts/qualify_release.py` (`REQUIRED_JOURNEYS`)
and the Phase 27 register. The Phase 22 "22 journeys" line is
superseded; older summaries remain historical.

**Workload denominator (frozen for `scripts/workload_measurement.py`):**
health_probe ×10, deterministic_echo ×10, model_asset ×4, link_preview
×4, missing_tool_build ×1, cross_tenant_control ×2 (31 planned items,
concurrency 3). Hard gates: verified completion ≥ 0.85 (independent
per-family scorers), unauthorized = 0, duplicate effects = 0, abandoned
= 0; p50/p95/throughput/cost tracked. An unavailable effect ledger is
UNKNOWN (`effects_unavailable_runs`), never silent zero. Controlled
external writes and recovery episodes are evidenced by the dedicated
§32.5/§32.12 drills, not by this capacity workload. Any override
(`MEASURE_FAMILY_COUNTS`) is recorded in the receipt and cannot be
presented as the frozen run.

**Capability controls while unqualified:** generated standalone tools
gate build/validation/activation/dispatch independently
(`pending_review` never auto-enables); specialist stages gate delegation
(`require_role_stage`); hosted providers require per-profile
allowlisted provider/deployment/region/credential mode with fallback
needing its own authorization; marketplace declared-app install follows
J23; OBJ/PDF artifacts publish only after byte validation.

**Deliberately unassigned (pending owner):** cloud RTO/RPO objectives,
canary cohort sizes per cloud, pilot cohort, cost envelope per cloud.
These stay visibly pending and hold their dependent decisions.

