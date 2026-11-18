#!/usr/bin/env bash
# Safe Cloud Run deploy for Veyrune (API + worker + frontend).
#
# WHY THIS EXISTS: scripts/gcp/deploy-api-cloudshell.sh uses
# `gcloud run deploy --set-env-vars=...`, which REPLACES the service's whole
# env-var set — a redeploy of the live service would silently wipe
# CORS_ALLOWED_ORIGINS, FRONTEND_BASE_URL, and anything else set out-of-band.
# This script deploys IMAGE-ONLY (`gcloud run deploy --image`), which keeps
# every env var, secret, IAM binding, and scaling setting on the service.
#
# Prerequisites (once):
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>
#   gcloud auth application-default login   # not required for build+deploy
#
# Usage:
#   bash scripts/gcp/deploy-safe.sh                  # deploy everything
#   DEPLOY_FRONTEND=0 bash scripts/gcp/deploy-safe.sh  # backend only
#
# Overrides (all optional):
#   PROJECT_ID, REGION=us-central1,
#   API_SERVICE=veyrune-api, WORKER_SERVICE=veyrune-worker, FRONTEND_SERVICE=veyrune-frontend,
#   NEXT_PUBLIC_API_URL (frontend build-arg; defaults to the deployed API URL)
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: no active gcloud project/account. Run:"
  echo "  gcloud auth login && gcloud config set project <PROJECT_ID>"
  exit 1
fi

REGION="${REGION:-us-central1}"
DEPLOY_FRONTEND="${DEPLOY_FRONTEND:-1}"
DEPLOY_WORKER="${DEPLOY_WORKER:-1}"

echo "Project=${PROJECT_ID} Region=${REGION}"
gcloud config set project "${PROJECT_ID}" >/dev/null

# ── 1. Build backend images from source (Cloud Build, per cloudbuild.yaml) ──
echo "── Building API + worker images (Cloud Build) ──"
gcloud builds submit --config=cloudbuild.yaml \
  --project="${PROJECT_ID}" .

API_IMAGE="gcr.io/${PROJECT_ID}/agentos-api:latest"
WORKER_IMAGE="gcr.io/${PROJECT_ID}/agentos-worker:latest"

# ── 2. Discover live services (image-only deploy keeps their config) ────────
detect_service() { # $1 = substring to match
  gcloud run services list --project="${PROJECT_ID}" \
    --format="value(metadata.name)" 2>/dev/null | grep -i "$1" | head -1 || true
}

API_SERVICE="${API_SERVICE:-$(detect_service api)}"
WORKER_SERVICE="${WORKER_SERVICE:-$(detect_service worker)}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-$(detect_service frontend)}"

deploy_image() { # $1 service, $2 image
  local svc="$1" img="$2"
  if [[ -z "${svc}" ]]; then
    echo "!! No existing service matched '${1}' — skipping (create it once with the full-config script)."
    return 0
  fi
  echo "── Deploying image to ${svc} (config preserved) ──"
  gcloud run deploy "${svc}" \
    --image="${img}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet
}

deploy_image "${API_SERVICE}" "${API_IMAGE}"

if [[ "${DEPLOY_WORKER}" == "1" ]]; then
  deploy_image "${WORKER_SERVICE}" "${WORKER_IMAGE}"
fi

# ── 3. Frontend: build with the live API URL, then image-only deploy ────────
if [[ "${DEPLOY_FRONTEND}" == "1" ]]; then
  if [[ -z "${FRONTEND_SERVICE}" ]]; then
    echo "!! No frontend service found — skipping frontend deploy."
  else
    if [[ -z "${NEXT_PUBLIC_API_URL}" ]]; then
      API_URL="$(gcloud run services describe "${API_SERVICE}" --region="${REGION}" \
        --project="${PROJECT_ID}" --format='value(status.url)' 2>/dev/null || true)"
      NEXT_PUBLIC_API_URL="${API_URL%/}/api/v1"
    fi
    echo "── Building frontend image (NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}) ──"
    gcloud builds submit --config=cloudbuild.frontend.yaml \
      --substitutions="_API_URL=${NEXT_PUBLIC_API_URL}" \
      --project="${PROJECT_ID}" .
    deploy_image "${FRONTEND_SERVICE}" "us-central1-docker.pkg.dev/${PROJECT_ID}/veyrune/frontend:latest"
  fi
fi

echo
echo "Done. Now verify the P0 fixes live:"
echo "  bash scripts/gcp/verify-p0-production.sh"
