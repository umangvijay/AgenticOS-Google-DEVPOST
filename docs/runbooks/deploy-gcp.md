# Google Cloud staging release — Phase 26

AgentOS can be hosted on Google Cloud, but the full product has not been
qualified there. The current cloud deployment probe supports **GKE** with the
Helm chart, direct HTTPS ingress and single-platform pinned image digests.
Cloud Run examples elsewhere describe a different, narrower hosting profile
and cannot satisfy the GKE qualification stage.

## Current state and repaired entry point

`scripts/gcp/deploy-gcp-postgres.sh` now prepares release inputs; it does not
deploy a complete system. Without `--build` it only verifies/extracts the
candidate and writes a local build plan. With `--build` it builds and publishes
three application images to an **existing** Artifact Registry repository.

The previous script could not provide its claimed deployment: it resolved the
repository root to `scripts/`, used a nonexistent root frontend Dockerfile,
deployed mutable tags, printed a duplicated digest prefix, exposed the database
DSN in environment arguments, swallowed provisioning errors and connected
separate API/worker services with an in-process queue. It also deployed a pull
worker with no HTTP listener as a Cloud Run service, omitted durable artifact
and runner configuration, and did not actually run the advertised migration
step. Those provisioning commands have been removed rather than presented as a
working production path.

## 1. Freeze and review the source

Run `scripts/freeze_candidate.py` with the repository's Python environment.
Use its new candidate ID; do not copy a historical ID from a runbook. Review
the manifest, source archive and qualification evidence for that source.
The builder uses the shared archive verifier and never builds from the live
checkout. It rejects corrupted archives, unexpected members, altered content
or modes, mismatched identities and nonempty extraction destinations.

## 2. Prepare locally, without a cloud account

Use your intended project ID and public HTTPS API endpoint. Example references
below are placeholders, not configured resources:

```bash
.venv/bin/python scripts/gcp/prepare_release.py \
  --candidate YOUR_CANDIDATE_ID \
  --project YOUR_PROJECT_ID \
  --region us-central1 \
  --repository agentos \
  --api-url https://YOUR_STAGING_HOST/api/v1 \
  --output docs/evidence/gcp-staging/prepare-01
```

This default mode needs no Docker daemon, account credentials or network calls.
It produces `build-plan.json` and a receipt marked `prepared_only`. It does not
produce image digests, Helm image values or a passing deployment receipt.
Each attempt requires a fresh output directory; earlier failures remain intact.

The build contexts are the frozen archive root for API/worker and `frontend/`
for the frontend. `NEXT_PUBLIC_API_URL` is supplied at build time, because
changing a runtime variable cannot rewrite the browser bundle. The chosen
platform defaults to `linux/amd64`; match it to your staging nodes.

## 3. Publish images when the target is configured

Required: an authorized billing-enabled project, existing Docker-format
Artifact Registry repository, authenticated gcloud, configured Docker registry
credentials, and a working Buildx builder. Use scoped registry permissions.
The command does not enable APIs, change IAM, rotate secrets or create billing
resources. Configure registry authentication for your selected region using
`gcloud auth configure-docker REGION-docker.pkg.dev`.

Repeat the preparation command with a fresh output directory and `--build`.
Publishing can consume local build resources and registry storage; it is an
explicit operation. On failure, inspect that attempt's logs and receipt; do
not assume already published images were rolled back.

For each image, the tool retains Buildx metadata, the registry manifest and
image configuration. It compares the actual manifest hash with the builder's
digest and checks the platform and candidate label. An index, missing digest,
changed source, mismatched bytes or failed registry lookup prevents success.
Only after all three images pass does it write `image-values.json` with
digest-pinned references, including the optional browser/runner worker image.
These values are **image overrides only**, not a complete deployment profile.

The tool requests a single-platform manifest with automatic in-image SBOM and
provenance attestations disabled to match the current deployment readback
contract. Its retained receipt is an unsigned build observation. Signed build
attestation, dependency/base-image pinning, SBOM and vulnerability scanning
remain required release work; a source label alone is not provenance proof.
See [Buildx build metadata](https://docs.docker.com/reference/cli/docker/buildx/build/)
and [registry inspection](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/).

## 4. Complete the runtime topology before installation

### 4a. Cloud SQL / GCS infrastructure (Terraform overlay, 2026-11-16)

`terraform/environments/gcp-postgres/` now declares the GCP/PostgreSQL
target-matrix topology that previously had no definition: a VPC with
private services access and Cloud NAT, **Cloud SQL PostgreSQL 16**
(private-IP only, IAM database authentication via the API/worker service
accounts, automated backups + PITR), a **versioned GCS artifacts bucket**
(retention on noncurrent revisions, separate restore-verifier identity),
Cloud Run API with a **Cloud SQL Auth Proxy sidecar**, a Cloud Run
worker pool with the same sidecar, and the scheduler. `terraform
validate` passes against google provider 6.50. The module sources:
`terraform/modules/cloud_sql`, `terraform/modules/gcs_artifacts`.

```bash
# owner-supplied once: state bucket + project
gsutil mb -l us-central1 gs://agenticos-tfstate-gcp-postgres
cd terraform/environments/gcp-postgres
terraform init
terraform apply -var project_id=YOUR_PROJECT \
  -var api_image=us-central1-docker.pkg.dev/YOUR_PROJECT/agentos/api@sha256:… \
  -var worker_image=us-central1-docker.pkg.dev/YOUR_PROJECT/agentos/worker@sha256:…
```

The application connects over the proxy's `/cloudsql` unix socket with
an IAM-token DSN (see `locals.postgres_dsn`); migrations run from the
image's migration runner at startup — Terraform owns infrastructure,
not schema.

**Honest scope of this overlay:** it provisions infrastructure only.
Still open, per the target matrix: a GCS `ArtifactStore` backend is NOT
yet implemented in the application (the bucket exists; the code still
writes the local POSIX backend), the bus transport decision for Cloud
Run (NATS endpoint vs. Postgres-outbox pull) is unresolved, and no
`terraform apply` has been executed against a real project.

### 4a-bis. Deploy-critical corrections (2026-11-19 audit)

Four errors were found and fixed in the deploy path — audit against
`terraform/environments/gcp-postgres/` and `cloudbuild*.yaml`:

1. **`cloudbuild.yaml` pushed to Container Registry (`gcr.io/...`) —
   DEAD.** Every current project fails the push. Both build files now
   target Artifact Registry
   (`us-central1-docker.pkg.dev/${PROJECT_ID}/agentos/...`); the frontend
   build no longer targets the `veyrune` repository. Create the repo
   once: `gcloud artifacts repositories create agentos
   --repository-format=docker --location=us-central1`.
2. **The overlay enabled none of the APIs its own resources need.**
   `sqladmin`, `compute`, `servicenetworking`, `artifactregistry` are now
   `google_project_service` resources in the overlay (Cloud SQL, the VPC,
   private-services access and Artifact Registry all failed at apply).
3. **The bus transport was `nats` with no NATS deployment** — the API
   and the worker pool could not exchange a single event. Default is now
   `pubsub`: `terraform/modules/pubsub` already provisions every topic
   the app consumes (`agentos-workflow-events`, `agentos-scheduler-triggers`,
   `agentos-recovery-events`, `agentos-mcp-validation`) plus the
   `-worker-sub` pull subscriptions, and the overlay now grants the API
   and worker service accounts `pubsub.publisher/subscriber/viewer`.
4. **The Cloud SQL DSN relied on IAM token auth the app does not
   implement** (asyncpg needs a real password; the app never fetches
   IAM tokens as database passwords). The overlay now generates a
   password user, assembles the proxy-socket DSN, stores it in Secret
   Manager (`agenticos-postgres-dsn`) and wires it as a SECRET-BACKED
   `POSTGRES_DSN` env on both the API and the worker — never a plain
   var. The Cloud SQL instance also sets `cloudsql.iam_authentication=on`
   for the supplementary IAM users.
5. A **frontend Cloud Run service** was added (set `frontend_image`);
   `api_max_instances` defaults to **1** until the GCS artifact backend
   exists (a second instance serves downloads from its own filesystem).

### 4b. Remaining runtime work

The following remain required configuration/implementation and verification:

- A durable bus connecting API and worker, such as the chart's NATS JetStream.
  An in-process bus cannot deliver between these separate processes.
- A GCS-backed artifact store implementation (bucket provisioned in 4a;
  `backend/services/artifact_store.py` still implements the POSIX
  backend only — the contract is backend-neutral).
- Stable JWT/vault/password keys through Secret Manager (the secrets
  module defines the placeholders; values are owner-supplied).
- Browser routing, ownership and takeover across the deployed replicas.
- An approved generated-code execution topology. The current runner needs a
  Docker supervisor; the Helm `runner` Deployment alone does not implement
  dispatch or supply a Docker daemon. GKE nodes use containerd, so do not add an
  assumed host Docker socket. Implement a supported isolated executor or a
  separately secured, reachable supervisor and qualify cancellation/orphan
  cleanup there. [GKE runtime documentation](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/using-containerd)
- Runtime identity and access to an enabled Vertex model; real usage/error/
  cancellation/credential-lifecycle verification. Merely setting a model name
  is not evidence of a working provider.
- Ingress DNS/TLS, routing, secrets, readiness, resource limits, backup/restore,
  monitoring, cost budget, canary and rollback appropriate to the chosen scope.

Render and review the chart with both the image overrides and a reviewed
staging configuration. Do not install the default chart and infer that its
optional browser/runner roles or artifact storage are fully connected.

## 5. Qualify the deployed target

Follow [the qualification runbook](qualify.md): retain a reviewed target
manifest with candidate hash, exact image digests, cluster/context, namespace,
ingress and independent HTTPS challenge fixture. Keep the staging token in
the configured secret store. Run the `target_deployment` stage with
`TARGET_PROFILE=gcp-gke` after an authorized deployment exists.

That stage verifies deployment ownership, image readback and an ingress
workflow. It does not close cloud durability/recovery/load drills, hosted
providers, enabled-agent journeys, genuine pilot results or release approval.
No RL deferral or release-owner approval is inferred by this runbook.
