#!/usr/bin/env bash
# Build the §21.8 isolated-runner image (alpine + python3 + non-root user).
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -t agentos-runner:latest -f- . << 'DOCKERFILE'
FROM alpine:3.19
RUN apk add --no-cache python3 py3-pip
RUN adduser -D -u 1000 runner
DOCKERFILE
echo "agentos-runner:latest ready"
