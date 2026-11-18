# Upgrade & Rollback Runbook (Phase 22 §22.31 — TESTED)

Executed 2026-11-07 on the live compose `core` stack, multiple upgrade
cycles (each Phase 22 defect-fix rebuild = one real upgrade with
persisted data) plus an explicit rollback drill. Data survived every
cycle: 123 → 125 workflows across the day's runs, zero integrity
failures.

## Upgrade (local containers)

```bash
# 0. Tag the running images as the rollback point FIRST
docker tag agentos-api:latest agentos-api:pre-upgrade-$(date +%Y%m%d-%H%M)
docker tag agentos-worker:latest agentos-worker:pre-upgrade-$(date +%Y%m%d-%H%M)

# 1. Build the new version
docker compose --profile core build api worker frontend

# 2. Rolling replace (compose recreates only changed containers)
docker compose --profile core up -d api worker

# 3. Health gate — do not proceed past a failing gate
curl -fsS http://127.0.0.1:8000/health
docker ps --filter name=agentos-api --format '{{.Status}}'   # healthy
# Startup applies pending migrations (ledger-checked; edits refused).
```

In-flight work: queued tasks survive because their state is durable
(tasks/commands/outbox rows). Proven live: the §21.2/§21.10 restart-
resume journeys re-run green after restarts, and this qualification's
live E2E (13/13) ran against a stack that had just been upgraded.

## Rollback (application only)

```bash
docker tag agentos-api:pre-upgrade-YYYYMMDD-HHMM agentos-api:latest
docker tag agentos-worker:pre-upgrade-YYYYMMDD-HHMM agentos-worker:latest
docker compose --profile core up -d api worker
curl -fsS http://127.0.0.1:8000/health
```

**Schema note (tested):** migrations are additive and remain applied —
an older image boots against a newer schema WITHOUT failing (verified:
rolled-back API healthy, 182 routes, data readable). Schema ROLLBACK
(0015/0016 DROPs) is intentionally NOT supported: never remove columns
a newer writer may have populated. Data-level restoration instead:
restore the pre-upgrade `pg_dump` into a fresh DB (see
[backup-restore.md](backup-restore.md)) and point the stack at it.

## Connector & policy rollback (independent of application rollback)

Connector revisions and policy versions carry their own identities:

```bash
# List revisions for a connector (revisions table, §12)
docker exec agentos-postgres-1 psql -U agentos -d agentos_test -t -c \
  "SELECT id, created_at FROM mcp_tool_revisions ORDER BY created_at DESC LIMIT 5;"
# Re-activate a previous revision through the API (production path):
#   GET /api/v1/integrations/{mcp_id} → revision history
#   POST /api/v1/integrations/{mcp_id}/revisions/{revision}/activate
```

Promoted policies roll back through the improvement service's governed
path (§21.17: `test_phase20_evolution.py` — rollback restores the
previous effective version and records the reason).

## Interrupted / repeated migration safety (tested)

- Repeated: `test_migrations_apply_idempotently_and_ledger_records_checksums`
  (9/9 postgres suite) — second apply is a no-op; checksum edits are
  refused by the ledger.
- Interrupted: each migration runs in its own transaction; an
  interrupted apply leaves earlier migrations recorded, and startup
  resumes from the ledger. Exercised live when the first 0014 attempt
  failed mid-startup (UndefinedColumnError) — the ledger recorded only
  completed migrations and the corrected successor applied cleanly.

## What this does NOT establish

- Kubernetes upgrade/rollback (helm install → upgrade → helm rollback):
  chart lints + renders (`helm lint`/`helm template`); a kind cluster
  execution is BLOCKED (no kind binary on host — runnable command in the
  evidence packet).
- Downgrade to a schema-erasing older version (unsupported by design).
