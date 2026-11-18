#!/usr/bin/env bash
# Reproducible local bootstrap (§21.1). Idempotent: safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "── AgentOS local bootstrap ──"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null; then
  echo "ERROR: python3 not found — install Python 3.11+ first" >&2; exit 2
fi

# 1. Virtualenv + pinned dependencies (only when missing/incomplete).
if [ ! -x .venv/bin/python ]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# 2. Playwright browsers (backend browser execution + e2e).
.venv/bin/python -m playwright install chromium

# 3. Frontend dependencies + browsers.
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm ci --no-audit --no-fund)
fi
(cd frontend && npx playwright install chromium)

# 4. Environment file (never printed).
if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "Created .env from .env.example — add provider keys if you have them."
fi

# 5. Writable data directories.
mkdir -p data artifacts backend/security/keys

# 6. Doctor gate.
.venv/bin/python scripts/local_doctor.py
