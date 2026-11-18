# Production readiness

Updated 31 October 2026. Committed review baseline: `3ef8063` (v5.5.0).
**The system is not certified for unrestricted public or enterprise production
use.** Remaining gates include implementation, concurrency, full-stack behavior,
and operational evidence. Deployment alone does not close them. Previous
percentage scores and “only deploy remains” wording are superseded.

## Evidence boundary

Authentication, workflows, credentials, connector generation, browser automation,
recovery and adaptive routing are implemented to varying degrees. The
[release evidence packet](RELEASE_EVIDENCE.md) preserves earlier reported results
with qualifications. This documentation update did not rerun application suites,
build images, deploy services, or certify new working-tree changes.

The preceding focused review reproduced uncommitted SQLite usage, modified
standalone artifacts retaining deployable status, and stale connector coverage
with isolated in-memory checks. The Phase-2 budget work supersedes the usage
finding: accounting now runs on dedicated-connection `BEGIN IMMEDIATE`
transactions with an identity-based reservation contract
([budget-accounting.md](budget-accounting.md)); the remaining cost gate is
deployed-replica and emulator evidence, not source fixes.

## Required gates for the selected launch scope

| Gate | Required outcome | Roadmap |
|---|---|---|
| Identity and tenancy | Acceptance-only membership, atomic invitations, current authorization and resource isolation | 01 |
| Cost and abuse | **Phase 2 implemented**: identity-based durable reservations with atomic multi-scope admission, idempotent settlement, reconciliation, authoritative snapshots, and metered coverage of every paid path. Emulator parity proven (21/21, 1 Nov 2026). Remaining: deployed-replica evidence | 02 |
| Capacity & abuse control | **Phase 3 implemented**: cross-replica shared rate limiting (epoch windows, zero-limit refusal, batched safe cleanup), typed admission decisions with per-workload outage modes (503 fail-closed for expensive work), capacity leases with fencing/TTL recovery, durable command admission with tenant pending caps, bounded local bus, Pub/Sub flow control, atomic schedule claims, provider backoff, frontend overload states, admin metrics. Remaining: deployed multi-replica evidence; Cloud Armor/ingress verification per §3.18 | 03 |
| Durable execution | Persist/publish recovery, fenced leases, duplicate delivery and cancellation | 04 |
| External effects | Approval binding, provider idempotency and uncertain-outcome reconciliation | 05 |
| Connectors | Revision/credential-bound evidence and accurate coverage | 06 |
| Generated code | Proven isolated runner and immutable tested artifacts, or explicit exclusion | 07 |
| Browser | Cleanup, session ownership, action authorization and supported DOM journeys, or explicit exclusion | 08–09 |
| Completion | Observable outcomes and valid authorized artifacts | 10, 12, 17 |
| Frontend | Real-backend journeys, session races, reconnects, accessibility and fallback | 18–19 |
| Operations | Candidate images, scans, load/recovery/restore/rollback drills and alert delivery | 20 |

## Launch procedure

1. Define supported user journeys and exclusions in a versioned release scope.
2. Close required source findings and store/runtime-specific regression tests.
3. Build candidate images; retain digests and dependency information.
4. Exercise the frontend against the actual staging backend.
5. Run provider and failure drills using controlled identities and data.
6. Record evidence, limitations, operator ownership, stop conditions and rollback.
7. Start with a limited cohort and expand when measured objectives hold.

Choose reliability, latency, cost, recovery-time and data-loss objectives for
the intended workload before testing. A health request does not prove worker
execution, real OAuth, provider writes, or backup restoration.

Advanced self-improvement, broader RL, optional subagents, 3D workflow views,
SSO/provisioning and extra output formats are separate product phases. A smaller
supported product may launch after its required gates pass. Formal certification
requires a named standard and assessment; no such assessment is claimed here.

See the [detailed roadmap](full-stack-roadmap.md),
[current status](implementation-status.md), and [runbook](ops-runbook.md).
