# Deploy Sandtable to Google Cloud with Vertex AI

This guide publishes the current application; it does not certify production readiness. See [implementation status](implementation-status.md) and [remaining release gates](production-readiness.md). Run commands in Cloud Shell, in order. No command below upgrades your billing account.

## 1. Keep your free trial

Select your existing project linked to your **Free Trial billing account**. Do not click **Activate / Upgrade** if you do not want a paid account. Confirm remaining credit and expiry in Billing first. Google currently provides $300 for 90 days; without upgrading, resources stop when either runs out. Trial accounts cannot request quota increases or use managed partner models. This guide uses **Google models through Vertex AI**. Individual model access still needs verification in your project.

**Policy checked November 18, 2026:** accounts created before November 16, 2026 can continue using their trial credits for the Vertex/Gemini API while staying on the trial. Accounts created on or after November 16 cannot use the welcome credit for these services. Upgrading an older trial on or after that date also removes this credit coverage. See the [Google billing change FAQ](https://firebase.google.com/docs/ai-logic/faq-and-troubleshooting). Do not assume an upgrade preserves Vertex credit coverage.

Source: [Google Cloud trial terms and restrictions](https://docs.cloud.google.com/free/docs/free-cloud-features).

Set a Billing budget with alerts at 50%, 80%, and 90% of your remaining credit, excluding credits from the reported gross cost. Alerts are delayed notifications, not a spending cap. The pull worker runs continuously and consumes credit while idle. Use a small test deployment first; do not invite public users until release checks pass.

## 2. Get the code and enable services

```bash
export PROJECT_ID='YOUR_PROJECT_ID'
export REGION='us-central1'
gcloud config set project "$PROJECT_ID"
git clone https://github.com/umangvijay/AgenticOS-Google-DEVPOST.git SANDTABLE
cd SANDTABLE
export RELEASE="$(git rev-parse --short HEAD)"
export IMAGE_ROOT="$REGION-docker.pkg.dev/$PROJECT_ID/veyrune"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com aiplatform.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com pubsub.googleapis.com
gcloud artifacts repositories create veyrune --repository-format=docker --location="$REGION"
```

For an existing repository/resource, inspect and reuse it; do not delete it to rerun creation commands. If enabling a service prompts you to upgrade billing, stop and check eligibility; this guide does not require consenting to that upgrade.

## 3. Runtime identities and persistent storage

```bash
gcloud iam service-accounts create veyrune-api
gcloud iam service-accounts create veyrune-worker
export API_SA="veyrune-api@$PROJECT_ID.iam.gserviceaccount.com"
export WORKER_SA="veyrune-worker@$PROJECT_ID.iam.gserviceaccount.com"
for sa in "$API_SA" "$WORKER_SA"; do
  for role in roles/aiplatform.user roles/datastore.user; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:$sa" --role="$role" --condition=None
  done
done
# Skip if the (default) Firestore database already exists.
gcloud firestore databases create --database='(default)' --location="$REGION" --type=firestore-native
for topic in agentos-workflow-events agentos-scheduler-triggers agentos-recovery-events; do
  gcloud pubsub topics create "$topic"
  gcloud pubsub subscriptions create "$topic-worker-sub" --topic="$topic" --ack-deadline=600
  for sa in "$API_SA" "$WORKER_SA"; do
    gcloud pubsub topics add-iam-policy-binding "$topic" \
      --member="serviceAccount:$sa" --role=roles/pubsub.publisher
  done
  gcloud pubsub subscriptions add-iam-policy-binding "$topic-worker-sub" \
    --member="serviceAccount:$WORKER_SA" --role=roles/pubsub.subscriber
done
```

Use Firestore for both API and worker. SQLite on Cloud Run is ephemeral. Keep the existing `agentos-` topic names: these are internal protocol names, independent of product branding. Review `firestore.indexes.json` and create any required composite indexes before testing queries.

Cloud Build's build identity needs Artifact Registry Writer, Logs Writer, and access to its source bucket. Determine the actual identity with `gcloud builds get-default-service-account`; grant those roles to that identity if a build reports permission denied. The person deploying also needs permission to act as the runtime identities. Do not solve IAM errors by granting Owner to runtime services.

## 4. Create secrets once

Never replace an existing vault master key or password pepper during an ordinary redeploy. Existing ciphertext/passwords depend on them. Use Secret Manager versions and a planned migration for rotation. Existing installations must retain their current secrets.

For a **new installation**:

```bash
umask 077
SECRET_DIR="$(mktemp -d)"
python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | \
  gcloud secrets create secrets-master-key --data-file=-
python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' | \
  gcloud secrets create password-pepper --data-file=-
openssl genrsa -out "$SECRET_DIR/private.pem" 3072
openssl rsa -in "$SECRET_DIR/private.pem" -pubout -out "$SECRET_DIR/public.pem"
gcloud secrets create jwt-private-key --data-file="$SECRET_DIR/private.pem"
gcloud secrets create jwt-public-key --data-file="$SECRET_DIR/public.pem"
rm "$SECRET_DIR/private.pem" "$SECRET_DIR/public.pem"
rmdir "$SECRET_DIR"
for secret in secrets-master-key password-pepper jwt-private-key jwt-public-key; do
  for sa in "$API_SA" "$WORKER_SA"; do
    gcloud secrets add-iam-policy-binding "$secret" \
      --member="serviceAccount:$sa" --role=roles/secretmanager.secretAccessor
  done
done
```

Do not put provider keys or service-account JSON files in GitHub. Vertex uses the attached service identity automatically. Previously shared keys should be revoked at their providers; they are not needed here.

## 5. Choose and verify a Vertex model

In Vertex AI Model Garden, choose a currently available Google Gemini model supporting text, JSON output and function calling. Prefer a stable Flash model for initial cost-controlled testing. Consult the [current model catalog](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/models). Newest does not automatically mean enabled, cheapest, or compatible with this application's SDK.

The current code defaults to `gemini-3.8-flash`. Its selectable text/tool models also include `gemini-4-hydrogen`, `gemini-4-argon`, `gemini-3.1-pro-preview`, `gemini-3.7-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, and `gemini-3.1-flash-lite`. The SDK requirement is `google-genai==2.34.0` for Gemini 4. Model IDs/configuration are not proof of project access or tool-call compatibility: probe each model and verify deployed agent workflows. Flash 3.6 is omitted because its documented retirement is November 19, 2026; Flash 3.7 retires January 28, 2027. Consult the [model lifecycle](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/model-versions) and [Gemini 4 migration guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/get-started-with-gemini-4).

Multiple specialized agents can share these models: an allow-list enables model selection/fallback, but does not assign a different model to every agent. Do not assume preview/partner models are covered. The generation location is `global`; verify regional availability if you change it. Embeddings use `gemini-embedding-001` in `us-central1`. The deployment example below is intentionally restricted to two models until the other choices have been verified in your project; expand `GEMINI_ALLOWED_MODELS` on both API and worker after successful probes. Cost estimates for models without a configured tariff are unavailable in-app; consult Cloud Billing for actual charges.

```bash
export MODEL='gemini-3.8-flash'
# This tests your Cloud Shell identity; the deployed workflow test later checks runtime identity.
curl --fail-with-body \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H 'Content-Type: application/json' \
  "https://aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/global/publishers/google/models/$MODEL:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"Reply with OK"}]}],"generationConfig":{"maxOutputTokens":64}}'
```

Stop on 403/404: verify project, model access, IAM and location. A 429 requires lower concurrency/backoff or available capacity, not another API key. Vertex does not guarantee unlimited inference. Do not upgrade the trial to bypass a restriction without deciding that separately.

## 6. Build and deploy API + pull worker

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions="COMMIT_SHA=$(git rev-parse HEAD),_API_IMAGE=$IMAGE_ROOT/api:$RELEASE,_WORKER_IMAGE=$IMAGE_ROOT/worker:$RELEASE"
# Use a custom delimiter because GEMINI_ALLOWED_MODELS contains commas.
export COMMON_ENV="^@^APP_ENV=production@STORAGE_BACKEND=firestore@MESSAGE_BUS_TRANSPORT=pubsub@LLM_PROVIDER=vertex@GOOGLE_CLOUD_PROJECT=$PROJECT_ID@GOOGLE_CLOUD_REGION=global@GEMINI_MODEL=$MODEL@GEMINI_ALLOWED_MODELS=$MODEL,gemini-3.5-flash-lite@GEMINI_EMBEDDING_MODEL=gemini-embedding-001@GEMINI_EMBEDDING_REGION=us-central1"
export SECRET_BINDINGS='SECRETS_MASTER_KEY=secrets-master-key:latest,PASSWORD_PEPPER=password-pepper:latest,JWT_PRIVATE_KEY=jwt-private-key:latest,JWT_PUBLIC_KEY=jwt-public-key:latest'
gcloud run deploy veyrune-api --image="$IMAGE_ROOT/api:$RELEASE" \
  --region="$REGION" --service-account="$API_SA" --allow-unauthenticated \
  --min-instances=0 --max-instances=2 --cpu=1 --memory=1Gi --timeout=300 --port=8080 \
  --set-env-vars="$COMMON_ENV@RUN_WORKER=0" --set-secrets="$SECRET_BINDINGS"
export API_URL="$(gcloud run services describe veyrune-api --region="$REGION" --format='value(status.url)')"
gcloud run worker-pools deploy veyrune-worker --image="$IMAGE_ROOT/worker:$RELEASE" \
  --region="$REGION" --service-account="$WORKER_SA" --instances=1 --cpu=1 --memory=2Gi \
  --set-env-vars="$COMMON_ENV@WORKER_ONLY=1" --set-secrets="$SECRET_BINDINGS"
curl --fail "$API_URL/health"
```

The API is reachable publicly so the browser can call it; protected routes still require application authentication. The worker is a **Worker Pool**, not an HTTP Cloud Run service: it has no listening port. If your trial/project cannot deploy a Worker Pool, stop; deploying only the API will leave automation incomplete. Do not declare a healthy API proof that workflows work.

## 7. Build and deploy the website

```bash
gcloud builds submit --config cloudbuild.frontend.yaml \
  --substitutions="_FRONTEND_IMAGE=$IMAGE_ROOT/frontend:$RELEASE,_API_URL=$API_URL/api/v1"
gcloud run deploy veyrune-frontend --image="$IMAGE_ROOT/frontend:$RELEASE" \
  --region="$REGION" --allow-unauthenticated --min-instances=0 --max-instances=2 \
  --memory=512Mi --cpu=1 --port=8080
export WEB_URL="$(gcloud run services describe veyrune-frontend --region="$REGION" --format='value(status.url)')"
gcloud run services update veyrune-api --region="$REGION" \
  --update-env-vars="FRONTEND_BASE_URL=$WEB_URL,CORS_ALLOWED_ORIGINS=[\"$WEB_URL\"]"
printf '%s\n' "$WEB_URL"
```

The API URL is baked into the frontend build. Changing only a runtime frontend variable will not update it. For a custom domain, update frontend links, API CORS, and any OAuth redirect URI. Configure email/OAuth separately if you enable those login paths; secrets alone do not provision an email provider.

## 8. Verify before accepting users

Sign up/sign in, send a chat, reload to verify history, create a harmless public HTTP MCP connector, run it, then run a workflow that creates a missing connector and uses it. Check approval/rejection, cancellation, restart recovery, tenant isolation, artifact downloads and embedding-backed memory. Inspect Operations and worker logs.

For the automated deployed test, use a short-lived **application access token** from your signed-in test user (not a Google token):

```bash
read -rs -p 'Application access token: ' VEYRUNE_TEST_TOKEN; echo
export VEYRUNE_TEST_TOKEN
python3 scripts/verify_deployed_workflows.py --api-url="$API_URL/api/v1" --mcp
unset VEYRUNE_TEST_TOKEN
```

This creates real persisted test runs and consumes inference credits. It does not silently approve actions. A pending approval needs review in the UI. A failure is a deployment gate, not something to hide with demo output. Browser Chromium runs server-side; a streamed interactive desktop is not automatically provided by Cloud Run.

## 9. Updates, rollback, and cost control

**Repeat deploys:** discover the existing service/pool names and use them
explicitly. Build images tagged with the new commit, then deploy the API and
frontend using `gcloud run deploy SERVICE --image=IMAGE --region=REGION`.
Deploy the worker using `gcloud run worker-pools deploy POOL --image=IMAGE
--region=REGION`. The legacy `deploy-safe.sh` treats the worker as an HTTP
service and should not be used for this Worker Pool topology. Use
`--update-env-vars` only for settings intentionally changed; `--set-env-vars`
replaces the entire environment. Preserve existing secrets, service accounts,
database, OAuth configuration, and URLs. Rebuild the frontend with both its API
URL and the existing `_GOOGLE_CLIENT_ID` substitution when Google sign-in is used.
After deploying, run `python3 scripts/gcp/verify-p0-production.py` and see
`docs/ops-runbook.md` for rollback and incident playbooks.

For updates: `git pull --ff-only`, set `RELEASE` to the new commit, rebuild the three images and repeat deploy commands using the same secrets, service identities and database. Record previous image digests and service revisions before updating. Roll HTTP traffic back to a known revision with `gcloud run services update-traffic SERVICE --to-revisions=REVISION=100 --region="$REGION"`; redeploy the previous worker image separately. Database changes may require a migration rollback plan.

To pause test execution, scale the Worker Pool to zero in Cloud Run and stop submitting workflows. API/frontend with minimum zero scale down when idle; storage, builds and retained images can still consume credit. Delete unused images and review Billing regularly. Back up Firestore and test restoration before a public launch. Keep exports before the trial ends; without upgrading, Google stops resources at expiry/exhaustion.

Remaining production gates include live project verification, load/failure testing, monitoring/alerts, recovery drills, hardened execution isolation, and independent security review. See [production readiness](production-readiness.md).
