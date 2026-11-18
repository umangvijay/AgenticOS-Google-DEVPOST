# Local production (§20.28)

AgentOS runs its full journey locally — frontend, backend, durable
execution, recovery, learning, browser automation — without cloud
deployment. Cloud is a port, not a requirement.

## One-time bootstrap

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # §20.15 real browser execution
cd frontend && npm install && npx playwright install chromium
```

`python scripts/local_doctor.py` (§20.1) verifies every prerequisite and
names the specific fix for anything missing; `--json` gives the
machine-readable report. Exit 0 = ready, 1 = fixable gaps, 2 = unusable
environment.

## Running the stack

```bash
# Backend (SQLite storage, in-process bus — the local default)
.venv/bin/python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000

# Frontend
cd frontend && npm run dev
```

- App: http://localhost:3000 · API: http://127.0.0.1:8000/health
- All journeys work without provider credentials: deterministic
  engineering (planning, validation, state machines, `core.*` agents)
  runs locally; model inference needs a key in Settings (see
  [provider-authentication.md](provider-authentication.md)).

## Compose profiles (§20.2)

`compose.yaml` defines 9 services on 6 opt-in profiles — start only
what a journey needs:

| Profile | Services | For |
|---|---|---|
| core | api, worker, nats, postgres | durable stack start/stop/restart |
| browser | browser-worker | Playwright sessions off the API process |
| runner | runner (cap_drop ALL, no-new-privileges) | §20.14 isolated execution |
| local-ai | ollama | §20.7 local inference |
| emulators | firestore-emulator (+nats) | §20.13 emulator acceptance |
| observability | prometheus | metrics |

```bash
docker compose --profile core up          # durable stack
docker compose --profile emulators up firestore-emulator
```

Start/stop/restart preserves state: named volumes (`agentos-data`,
`agentos-artifacts`, `nats-data`, `postgres-data`) and healthchecks on
every long-running service.

## The §20 gates and how to run them

| Gate | Command |
|---|---|
| 20.4 PostgreSQL contract | `pytest backend/tests/test_phase20_postgres.py -q` (server on :55432 via compose core) |
| 20.5 JetStream messaging | `pytest backend/tests/test_phase20_nats.py -q` (server on :4222 via compose core) |
| 20.6 artifact store | `pytest backend/tests/test_phase20_artifact_store.py -q` |
| 20.12 real-backend browser journey | `cd frontend && npx playwright test e2e/local-backend.spec.ts` (backend on :8000) |
| 20.13 Firestore emulator acceptance | `FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 pytest backend/tests/test_phase20_firestore_emulator.py -q` |
| 20.14 isolated runner | `pytest backend/tests/test_phase20_runner.py -q` |
| 20.15 real browser execution | `pytest backend/tests/test_phase20_browser_service.py -q` |
| 20.16 viewer/takeover | `pytest backend/tests/test_phase20_viewer.py -q` + `cd frontend && npx vitest run tests/mounted/browser-session-panel.test.tsx` |
| 20.18 restart recovery | `pytest backend/tests/test_phase20_react.py -q` |
| 20.23 RL training | `pytest backend/tests/test_phase20_rl.py -q` (~30 s, real PPO) |

Every gate is also probed by the register: `pytest
backend/tests/test_phase20_register.py -q`. The full completion matrix
with per-gate evidence lives in
[docs/evidence/phase-20.md](evidence/phase-20.md).

## What is honest about local mode

- No canned outputs: a journey with no model key fails loudly at the
  inference boundary rather than returning a demo answer
  (`backend/tests/test_no_canned_outputs.py`).
- Blocked gates name their runnable command — nothing hides behind a
  silent skip (see the Firestore/NATS/Postgres suites' skip messages).
- The runner executes generated workloads in a credential-free,
  resource-limited jail; the browser service enforces origin scope;
  viewer/takeover is server-enforced.
