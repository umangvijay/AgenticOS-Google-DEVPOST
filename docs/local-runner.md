# The local runner (§20.14)

Generated code never runs with the worker's authority. The local runner
(`backend/runners/local_runner.py`) is a subprocess jail: real
execution, bounded blast radius, no credentials.

## Controls

| Control | Mechanism |
|---|---|
| Time limit | `asyncio.wait_for` + process-group kill (`SIGTERM`→`SIGKILL` to `-pgid`) — an infinite loop is killed, verified by test |
| Output cap | stdout/stderr truncated at a fixed byte cap (test-verified) |
| Credential-free env | allowlist environment (`PATH`, `LANG`, `TMPDIR`, …) — no API keys, no `GOOGLE_*`, no `AWS_*` (test asserts their absence) |
| Working-dir jail | fresh temp dir per run; forbidden paths (repo root, `.env`, vault) are pre-checked and recorded on violation |
| Resource limits | `setrlimit` per limit, applied GUARDED (macOS rejects `RLIMIT_AS` below the runtime mapping — each limit degrades independently instead of crashing the jail) |
| Cancellation | cooperative `cancel()` kills the process group immediately |
| Cleanup | the jail directory is removed after the run (test-verified) |

## Run it

```python
from backend.runners.local_runner import LocalRunner
runner = LocalRunner(time_limit_seconds=10, output_cap_bytes=65536)
result = await runner.run(["python", "solution.py"], input_data={"n": 3})
result.exit_code, result.stdout, result.timed_out, result.violations
```

## Evidence

`pytest backend/tests/test_phase20_runner.py -q` — 8 tests: success,
infinite-loop kill, output truncation, forbidden-path recording,
credential-free env, cancellation, rlimits, jail cleanup.

For container deployment the same contract is enforced at the pod level
(helm `runner` deployment: `runAsNonRoot`, `allowPrivilegeEscalation:
false`, `capabilities.drop: [ALL]`; compose `runner` profile:
`cap_drop: [ALL]`, `no-new-privileges: true`).
