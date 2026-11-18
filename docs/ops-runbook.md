# Sandtable — Operations Runbook

Production topology on Cloud Run: `veyrune-api` (FastAPI, RUN_WORKER=0),
`veyrune-worker` (separate consumer, Pub/Sub), `veyrune-frontend`
(Next.js standalone), Firestore, Pub/Sub, Secret Manager.

**Verification note — 31 October 2026:** confirm actual project, resource type,
image digest, configuration and permissions before using any command. This
document is not evidence that a drill passed. Record outcomes in
[RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md); roadmap phase 20 defines acceptance.

## Deploy

```bash
gcloud auth login && gcloud config set project <PROJECT_ID>
bash scripts/gcp/deploy-safe.sh          # image-only; preserves env/secrets/IAM
python3 scripts/gcp/verify-p0-production.py https://<veyrune-api-url>
```

Never redeploy with `scripts/gcp/deploy-api-cloudshell.sh` against an
existing service: its `--set-env-vars` wipes the service's env config.

## Verify health after any deploy

```bash
curl -s https://<api-url>/api/v1/health | jq .
# expect: status=healthy, factory_initialized=true, storage=firestore,
#         llm_status.status in (catalog_ok, vertex), commit=<revision sha>
```

`llm_status=missing` means no Gemini/Vertex credentials — LLM-dependent
features degrade with honest errors; LLM-free actions (site health, GET,
OpenAPI builds) keep working.

## Rollback

Cloud Run keeps every revision:

```bash
gcloud run services describe veyrune-api --region us-central1 \
  --format='value(status.traffic[0].revisionName)'   # current
gcloud run services update-traffic veyrune-api --region us-central1 \
  --to-revisions <PREVIOUS_REVISION>=100
```

Choose rollback or containment according to the incident. Additive schemas alone
do not guarantee old-code compatibility: event formats, policy, data semantics,
and migrations also matter. Test rollback against the actual candidate and data.

## Kill switch

`scripts/gcp-killswitch/` contains emergency-stop tooling that must be checked
against the deployed topology. The pull consumer is a Cloud Run Worker Pool,
not an HTTP service; do not use a service-scaling command as its stop procedure.

Before launch, rehearse and record resource-specific steps to stop new schedule
and API submissions, pause worker consumption, contain already-running browser
or provider actions, and verify no further expenditure. Preserve uncertain
effect records for reconciliation. Restart only after checking backlog and
duplicate-delivery behavior. Do not assume stopping intake cancels in-flight work.

## Runbook: common incidents

**429s spiking on /workflows** — inspect the specific refusal, user/tenant scope,
replica count, queue pressure and accounting health. Local counters and token
checks do not by themselves prove global enforcement. Do not raise limits before
identifying the source and verifying spending reservations.

**Model quality regressions** — inspect `GET /api/v1/usage/models` alongside
verified task outcomes. Runner success can differ from useful completion. Use
documented configuration/rollback procedures; no policy-reset API is implied here.

**Memory/JSON 5xx with hostile payloads** — should not happen: the
canonical JSON layer (`backend/security/json_safety.py`) plus surrogate
repair at `sanitize_text` cover storage and responses. If seen, capture
the payload and file against the sanitizer.

**Site-health misses on datacenter-blocked sites** — tune
`HEALTH_USER_AGENT`; a dedicated egress pool is the deploy-level fix.

**Scheduled runs piling up** — schedules have a max-concurrency guard
(skips with `SCHEDULE_SKIPPED` notifications) and skip-missed catch-up;
a backlog means the worker was down — check worker logs and Pub/Sub
subscription backlog.

## Data retention

`RETENTION_DAYS` (default 90) purges timeline events and audit rows in
the worker's daily pass. Workflow runs are never auto-deleted. Export
before large deletions if compliance requires it.

## Model selection (A/B guidance)

The observations below are historical, not current model recommendations.
Do not select or upgrade providers from these tiny samples. Phase 12 requires
representative outcomes; phase 14 adds controlled routing evaluation.

The adaptive policy (`GET /api/v1/usage/models`) re-ranks the model chain
from real task outcomes. Live A/B evidence from the audit (n=1 each,
indicative only):

- `gemini-3.8-flash` (default): planned both sites of a multi-part goal
  and wrote the synthesis (~26s).
- `gemini-4-argon`: ~2× faster (~12s) but dropped one site from the plan
  and skipped the synthesis.

Policy: keep `gemini-3.8-flash` as the default until a per-goal-template
A/B (≥20 runs each) shows argon planning multi-part goals reliably. The
bandit will surface the difference automatically in
`failed_tasks_by_agent` and the leaderboard once production traffic
accumulates — check it before changing defaults manually.

## Admission & capacity operations (Phase 3)

Inspect live enforcement:

```bash
curl -s https://<api-url>/api/v1/admin/admission/metrics \
  -H "Authorization: Bearer <admin-token>" | jq .
```

Watch two series:

- `rate_limiting.counters.local_fallback_activations{group=…}` — the shared
  admission store is ERRORING and strict cross-replica limits degraded to
  per-instance caps. Treat as an incident: check Firestore/DB health.
- `capacity.counters.leases_expired` — workers died holding capacity; the
  TTL + ticker reclaimed it. Repeated growth means worker crashes.

Denials carry a `decision_id`; correlate with access-controlled logs, never
metric labels. `ADMISSION_UNAVAILABLE` (HTTP 503) on workflow/build routes is
the fail-closed posture — restore the store instead of loosening limits.

Rate-limit numbers are settings (`RATE_LIMIT_*`); semantics are
`backend/security/admission_policy.py`. Changing a group's shape (algorithm,
failure mode) is a policy-version change — see docs/admission-and-capacity.md.

Ingress: when Cloud Armor fronts the API via a load balancer, set the
`cloud_run` module `ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"` so the
default `*.run.app` serverless URL cannot bypass the edge (§3.18). Verify no
other public endpoint reaches the service.

## Identity & trusted-proxy verification (Phase 3 §3.7)

Limiter identity for anonymous traffic is the ASGI peer (`request.client`),
never `X-Forwarded-*` headers (a caller can forge those). Verify per deploy:

1. Hit the API through the load balancer and confirm logs show the REAL
   client IP in `ip:<host>` identities, not the balancer's own address.
2. If client IPs collapse to one value behind a proxy, configure the
   proxy-headers middleware (`--proxy-headers` + `FORWARDED_ALLOW_IPS`) on
   the server instead of reading forwarded headers in application code —
   the middleware validates the peer, application code must not.
3. Confirm direct `*.run.app` access is either intended (ingress ALL) or
   blocked (see ingress note above) — a bypass path changes which peers the
   limiter sees.

## Phase 4 command-lifecycle rollout (§4.24)

Repository-side additive steps are COMPLETE (schemas/indexes are additive,
existing readers work, the webhook path migrated first, old and new dispatch
never handle the same accepted work — the webhook's legacy path was replaced
atomically). The DEPLOYMENT drill records evidence here as it executes:

1. Deploy the revision (additive schema migration runs on boot for SQLite;
   Firestore collections are created on first write).
2. Verify `GET /api/v1/commands/{id}` resolves for a fresh submission.
3. Kill the API process after acceptance, before the outbox drains: on
   restart the reconciler publishes the event and the work appears — the
   §4.5 acceptance guarantee.
4. Fire a webhook twice with the same signed body: the second receives the
   duplicate receipt (409/deduplicated), never a second run.
5. Cancel a running workflow: the UI shows requested → stopped → terminal,
   with completed-before-cancel returned honestly.
6. Rollback: previous revision + additive schema are compatible (old readers
   ignore new columns/tables); already-accepted commands remain readable and
   the reconciler drains their outbox on the old revision only if the command
   service is present there — verify before rolling back past v7.0.0.
