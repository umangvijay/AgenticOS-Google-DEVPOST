# Runbook — Runner Failure (Phase 7 §7.24)

Managed isolated execution is currently UNAVAILABLE (§7.5: no backend proven
against the isolation acceptance profile). This runbook activates when that
gate is opened.

## Orphaned executions (worker crash mid-run)

1. The worker ticker's `reconcile_orphans` marks executions whose lease
   expired while still running as `superseded` (exit reason "lease lost").
2. Their cleanup state moves pending → cleaned in the same pass.
3. Verify: `runner_executions` has no rows in
   (queued, starting, running) with expired leases.
4. If a sandbox survives in the runtime despite the record (§7.18 — killing
   the local client does not prove descendants ended), terminate it through
   the runtime's own console and record the incident.

## Non-cooperative workload (ignores cancellation)

1. `cancel_pending` records cancel_requested and stops new call admission.
2. After the bounded grace period, `mark_terminated` + supervisor-level
   termination.
3. Verify owned descendants ended (§7.18 acceptance).
4. Uncertain external effects keep their Phase 5 state — reconcile, never
   blind-retry.

## Artifact suspected malicious

1. `controls.revoke_artifact(digest, reason)` — blocks new dispatches.
2. `controls.stop_provider_access(reason)` — stops provider access without
   deleting evidence.
3. `cancel_tenant_executions(repo, tenant, reason)` for affected tenants.
4. Preserve all effect/validation evidence for the investigation.

## Runtime image vulnerable

1. `controls.revoke_runtime_image(image_digest, reason)`.
2. Drain the runner version: `controls.drain_runner_version(version, reason)`.
3. Rebuild on the patched base; the rebuilt image is a NEW digest requiring
   fresh validation attestations (§7.19 — old evidence cannot authorize it).
