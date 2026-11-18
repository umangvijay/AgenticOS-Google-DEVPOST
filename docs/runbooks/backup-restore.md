# Backup & Restore Runbook — Phase 23 recovery gate

**Current status: full service restore NOT YET QUALIFIED.** The Phase 22
observations below describe a database restore and volume mechanism, not
recovery of a separate working application. A passing database import or
an upgrade of the original stack cannot satisfy this gate.

The historical Phase 22 packet reported execution on 2026-11-07 against the compose
`core` stack. Measured on this host: dump 1.6 MB / <1 s, restore 2 s
(123 workflows, 125 tasks, 14 tools, 189 cached tool definitions,
61 reservations, 458 events), zero orphans, migration ledger intact
(17 entries).

## 1. Back up the complete recoverable state

```bash
set -euo pipefail
umask 077
mkdir -p backups
TS=$(date +%Y%m%d-%H%M%S)

# 1a. Database (pg_dump, custom dir)
docker exec agentos-postgres-1 pg_dump -U agentos agentos_test \
  > backups/db-$TS.sql

# 1b. Artifacts volume (files: deliverables, exports)
docker run --rm -v agentos_agentos-artifacts:/src -v "$(pwd)/backups":/dst \
  alpine sh -c "cd /src && tar czf /dst/artifacts-$TS.tgz ."

# 1c. These are JWT signing keys, not the vault master key.
# Confirm the configured JWT_PRIVATE_KEY_PATH/JWT_PUBLIC_KEY_PATH first;
# environment/secret-manager keys require their own versioned recovery path.
mkdir -p "backups/jwt-keys-$TS"
test -s backend/security/keys/private.pem
test -s backend/security/keys/public.pem
cp backend/security/keys/private.pem backend/security/keys/public.pem "backups/jwt-keys-$TS/"
```

The key filenames above are an example only: use the actual configured
paths. A missing key is a backup failure; do not suppress copy errors.
`SECRETS_MASTER_KEY` is the credential encryption material loaded by
`SecretsVault`; copying JWT files does not preserve it. Preserve the exact
master-key version through the deployment's encrypted secret backup or
secret-manager recovery procedure, and prove decryption after restore.
Do not export `.env` into an unencrypted archive. File mode 0600 restricts
access but is not encryption. Record key identifiers and versions in the
backup manifest without recording secret values.

Preserve NATS JetStream storage and consumer metadata, or demonstrate
complete replay from the outbox for every accepted command before claiming
it can be reconstructed. Include emitted-but-unacknowledged events and
outbox retention in that demonstration. The Compose NATS service stores
JetStream data in its mounted `/data` directory.

## 2. Restore into a SEPARATE clean environment

```bash
# Fresh database (same server here; in production use a new host)
docker exec agentos-postgres-1 psql -U agentos -d postgres \
  -c "CREATE DATABASE agentos_restore;"
docker exec -i agentos-postgres-1 psql -U agentos -d agentos_restore \
  -v ON_ERROR_STOP=1 < backups/db-$TS.sql

# Artifacts
docker run --rm -v agentos_agentos-artifacts-restore:/dst \
  -v "$(pwd)/backups":/src alpine sh -c \
  "mkdir -p /dst && cd /dst && tar xzf /src/artifacts-$TS.tgz"
```

Start a separately named candidate stack pointed at the restored database
and restored artifacts, using recovered secret versions. Inside a
container, the database hostname must be the restore service's network
name; `127.0.0.1` refers to that container, not the database host. Choose
separate ports and volumes. Verify the current migration ledger rather
than relying on a historical migration count. Do not overwrite the
source stack during this drill.

## 3. Post-restore integrity checks (must all pass)

```bash
# Counts match the source (recorded pairs above)
docker exec agentos-postgres-1 psql -U agentos -d agentos_restore -t -c "
 SELECT count(*) FROM workflows; SELECT count(*) FROM tasks;
 SELECT count(*) FROM pg_mcps; SELECT count(*) FROM mcp_tools;"

# No orphans (FK relationships survived)
docker exec agentos-postgres-1 psql -U agentos -d agentos_restore -t -c "
 SELECT count(*) FROM tasks t LEFT JOIN workflows w ON t.run_id=w.id
 WHERE w.id IS NULL;"
# → 0

# Migration ledger present; startup re-verification is a no-op
# Authorization: a restored user's token revocations still apply —
# pg_token_denylist rows are part of the dump.
```

With the original application unavailable, execute on the restored stack:

1. Log in as a restored user and verify revoked sessions are refused.
2. Decrypt a known test credential and use it against the controlled test app.
3. Download existing artifacts; compare each file hash and ownership denial.
4. Resume interrupted work; verify the result and absence of duplicate writes.
5. Invoke an activated connector and verify its actual postcondition.
6. Verify deletion tombstones and memory invalidation after replay.
7. Record start/end timestamps, candidate identity, backup identity, recovered
   data cutoff, commands, outputs, and all errors.

An E2E run against the original stack does not establish restored-service
behavior. No current full-stack restore pass is recorded here.

## 4. Measured recovery characteristics

### Repeat the local Phase 26 application drill

With the local Compose PostgreSQL service available on port 55432, run
`.venv/bin/python scripts/qualify_restore_application.py /tmp/phase26-restore.json`
from the repository root. Supply a fresh receipt path each time. The restored
workflow checks TLS on example.com and requires outbound access. The probe
creates two disposable databases on the same instance and starts each API with
its own data directory. It stops the source API before exporting the database,
signing keys, vault key and artifact files. The receipt records source and
restored counts, checks on authenticated application behavior, elapsed time
through the fresh workflow and the last acknowledged write before the snapshot.

Hosted qualification requires running on the selected recovery infrastructure
with its real worker transport, recoverable key store and storage volumes.
The local fixture keeps its archive encryption key in memory for one drill.

- RPO (recoverable data loss): committed transactions since the last
  dump. Run a dump on a schedule (cron) for a bounded RPO; the dump is
  hot (pg_dump uses snapshot isolation; safe while the stack runs).
- Historical database import timing is not full-service RTO. Measure RTO
  until the restored service passes the functional checks above.
- A backup predating a revocation or deletion can resurrect old state.
  Include a replay/invalidation strategy and explicitly test this case;
  rotating signing keys alone does not revoke every stored credential.

## 5. What this does NOT establish

- Cross-host restore (same docker engine was used) — the dump is plain
  SQL, portable to any Postgres 16; a full new-host drill is a Phase 23
  target.
- PITR/WAL archiving (not configured in Phase 22; declared limitation).
- Encrypted key-backup and full-service restore execution remain release gates.
