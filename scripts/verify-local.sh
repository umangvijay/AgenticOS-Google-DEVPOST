#!/usr/bin/env bash
# Repeatable local candidate gate. This does not certify a deployed service.
# Phase 00 §00.4: records the source identity and adds the fresh-database
# migration check + the emulator-gated Firestore parity stage.
set -euo pipefail
cd "$(dirname "$0")/.."
agentos_python="${AGENTOS_PYTHON:-.venv/bin/python}"
echo "── source identity (record in docs/RELEASE_EVIDENCE.md) ──"
echo "commit: $(git rev-parse HEAD 2>/dev/null || echo 'no-git')"
echo "dirty files: $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

echo "── [migration] fresh-database initialize ──"
VERIFY_DB="$(mktemp -d)/verify.db"
MIGRATION_DB="$VERIFY_DB" "$agentos_python" - <<'PYEOF'
import asyncio, os, sys
async def main():
    from backend.repositories.sqlite.database import DatabaseManager
    db = DatabaseManager(os.environ["MIGRATION_DB"])
    await db.initialize()
    required = ("rate_limits", "capacity_leases", "pending_commands",
                "commands", "outbox_events", "parent_waits", "effects",
                "effect_receipts", "browser_sessions", "execution_steps",
                "recovery_episodes", "evaluation_records", "experiments",
                "routing_decisions", "agent_tasks", "prompt_versions",
                "workflow_templates", "memories")
    cursor = await db._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in await cursor.fetchall()}
    missing = [t for t in required if t not in tables]
    if missing:
        print("FAIL: missing tables:", missing); sys.exit(1)
    print(f"migrations OK ({len(required)} required tables present)")
    await db.close()
asyncio.run(main())
PYEOF

echo "── [firestore parity] emulator-gated ──"
if [[ -n "${FIRESTORE_EMULATOR_HOST:-}" ]]; then
  GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-demo}" \
    "$agentos_python" -m pytest \
    backend/tests/test_phase8_11_firestore_parity.py \
    backend/tests/test_phase3_firestore_parity.py -q
else
  echo 'Firestore parity skipped: no FIRESTORE_EMULATOR_HOST (gcloud emulators firestore start).'
fi

"$agentos_python" --version
"$agentos_python" -m pytest backend/tests -q
"$agentos_python" -m pip check
npm --prefix frontend run lint -- --max-warnings=0
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run typecheck
if [[ "${AGENTOS_BROWSER_TESTS:-1}" == "1" ]]; then
  npm --prefix frontend run test:e2e:production -- --reporter=line
else
  echo 'Browser acceptance skipped: release gate remains incomplete.'
fi
