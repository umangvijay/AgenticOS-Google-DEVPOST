#!/usr/bin/env bash
# Compatibility entry point. Prepare a verified GKE image release; --build
# explicitly builds/pushes images. This no longer provisions Cloud Run/SQL.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
: "${CANDIDATE_ID:?set CANDIDATE_ID to a reviewed frozen candidate}"
: "${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
: "${NEXT_PUBLIC_API_URL:?set the planned HTTPS API URL ending in /api/v1}"
: "${RELEASE_OUTPUT:?set RELEASE_OUTPUT to a fresh local attempt directory}"
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then PYTHON_BIN=python3; fi
exec "$PYTHON_BIN" "$REPO_ROOT/scripts/gcp/prepare_release.py" \
  --candidate "$CANDIDATE_ID" --project "$GOOGLE_CLOUD_PROJECT" \
  --region "${GOOGLE_CLOUD_REGION:-us-central1}" \
  --repository "${ARTIFACT_REGISTRY_REPOSITORY:-agentos}" \
  --api-url "$NEXT_PUBLIC_API_URL" --output "$RELEASE_OUTPUT" "$@"
