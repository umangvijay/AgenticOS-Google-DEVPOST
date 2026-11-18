# Cloud portability (§20.27)

AgentOS is **local-first**: every journey (frontend/backend, mounted
React tests, Firestore emulator tests, isolated execution, browser
viewing/takeover, recovery, learning) runs without cloud deployment.
Portability is a deployment CHOICE, not a code rewrite.

## What is pluggable

| Concern | Local default | Cloud option | Switch |
|---|---|---|---|
| Repository layer | SQLite (`STORAGE_BACKEND=sqlite`) | PostgreSQL 16 (`backend/repositories/postgres/`, explicit migrations) or Firestore | `STORAGE_BACKEND` |
| Message bus | In-process bounded queue | NATS JetStream (durable stream, ack, bounded redelivery, DLQ) or GCP Pub/Sub | `NATS_SERVERS` / Pub/Sub env |
| Inference | Ollama (local, `ollama` compose profile) | Vertex AI (express key or ADC) / Bedrock (bearer or boto3 chain) | provider settings |
| Artifacts | Local filesystem store (`backend/services/artifact_store.py`) | same contract on GCS/S3 (implement the six ops) | inject a different store |
| Runner | Docker-supervised isolated containers for generated code; local subprocess runner is for trusted tooling | Requires a qualified cloud executor/supervisor topology; the Helm runner pod does not itself provide Docker dispatch | profile/deployment |

## The helm chart

The chart is a deployment foundation, not completed cloud qualification. See
the [GCP staging runbook](runbooks/deploy-gcp.md) for immutable image preparation
and outstanding runtime wiring. Do not assume its optional runner Deployment
can execute generated code merely because its pod starts.

`deploy/helm/agentos/` renders api, worker, browser-worker, runner,
frontend Deployments; NATS + PostgreSQL StatefulSets; Services and an
optional Ingress.

```bash
helm lint deploy/helm/agentos
helm template deploy/helm/agentos \
  --set storage.backend=postgres \
  --set storage.postgres.existingSecret=agentos-db
# install:
helm install agentos deploy/helm/agentos \
  --set storage.backend=postgres \
  --set storage.postgres.existingSecret=agentos-db
```

Rules encoded in the chart:

- **Credentials never live in values.yaml.** Provider keys
  (`GOOGLE_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`) and the
  DB DSN come from pre-existing Secrets (`secrets.external` / `envFrom`);
  the chart only references them.
- **The runner runs hardened**: `runAsNonRoot`,
  `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]` —
  mirroring the compose `runner` profile (`cap_drop: [ALL]`,
  `no-new-privileges:true`).
- **Postgres migrations are explicit** (`migrations/0001_core.sql`,
  checksum ledger). A migration is never edited after application;
  schema changes are new files.

## Postgres subset on cloud

The §20.4 core subset (users, workflows/runs, tasks with durable lease
claim, idempotent commands, ordered replayable events, revisioned
deliverables, budget reservations) is contract-tested against a live
Postgres 16:

```bash
docker run -d --name agentos-pg20 -p 55432:5432 \
  -e POSTGRES_PASSWORD=agentos -e POSTGRES_USER=agentos \
  -e POSTGRES_DB=agentos_test postgres:16-alpine
pytest backend/tests/test_phase20_postgres.py -q   # 8 tests, live server
```

## NATS on cloud

The §20.5 JetStream adapter (stream `agentos.>` subjects, `Nats-Msg-Id`
dedup, manual ack, max_deliver=5, dead-lettering to `agentos.dlq.*`,
ordered replay) is live-tested:

```bash
docker run -d --name agentos-nats20 -p 4222:4222 nats:2.10-alpine -js
pytest backend/tests/test_phase20_nats.py -q        # 11 tests, live server
```

## What deliberately does NOT change on cloud

- The no-canned-output rule: every journey consumes real model output or
  fails loudly.
- Deliverable verification/publishing contracts (per-file digests,
  revision-safe review, publication jobs with recovery).
- Effect idempotency (at-least-once transport + idempotent handlers).
- Anti-production guards: the Firestore emulator harness refuses to run
  with real credentials or against non-loopback endpoints.

## Firestore mode

`STORAGE_BACKEND=firestore` requires `GOOGLE_CLOUD_PROJECT` (and the
standard ADC chain). The repositories are parity-tested against the
emulator (`backend/tests/firestore_emulator_harness.py` — 48 parity
tests, zero skips allowed inside a guarded run).
