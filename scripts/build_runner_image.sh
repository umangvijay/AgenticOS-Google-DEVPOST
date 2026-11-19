#!/usr/bin/env bash
# Build the §21.8 isolated-runner image (alpine + python3 + non-root user).
# The definition lives in Dockerfile.runner — the same file CI
# (cloudbuild.yaml) builds so the runner-lifecycle contracts execute.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -t agentos-runner:latest -f Dockerfile.runner .
echo "agentos-runner:latest ready"
