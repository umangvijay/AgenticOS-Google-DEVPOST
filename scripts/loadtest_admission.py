"""Admission load test (Phase 3, §3.22 order 16).

Proves the SHARED fixed-window limiter holds under a real burst from
independent OS processes: N worker processes each attempt M admissions
against one SQLite store for the same identity with allowance K — exactly K
must be admitted across ALL processes, no matter how attempts interleave.

Also exercises capacity-lease contention: P processes race to acquire Q
units — exactly Q win.

Usage:
    python3 scripts/loadtest_admission.py [--workers 8] [--attempts 50]

This is a deterministic correctness-under-load check, not a latency
benchmark. Record its output in docs/RELEASE_EVIDENCE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import multiprocessing as mp
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _worker_rate(db_path: str, window_start: int, attempts: int) -> int:
    """Independent CONNECTION (not import-shared state): raw SQLite through
    the same guarded upsert the repository issues."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    wins = 0
    for _ in range(attempts):
        cursor = conn.execute(
            """
            INSERT INTO rate_limits
                (identifier, group_name, window_seconds, window_start,
                 window_end, count, cleanup_after, policy_version)
            VALUES ('loadtest', 'auth', 60, ?, ?, 1, ?, 'p3.v1')
            ON CONFLICT(identifier, group_name, window_seconds, window_start)
            DO UPDATE SET count = rate_limits.count + 1
            WHERE rate_limits.count < 25
            """,
            (window_start, window_start + 60, window_start + 120),
        )
        conn.commit()
        wins += 1 if cursor.rowcount else 0
    conn.close()
    return wins


def _worker_capacity(db_path: str, attempts: int) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    import asyncio

    from backend.repositories.capacity_repository import SQLiteCapacityRepository
    from backend.repositories.sqlite.database import DatabaseManager
    from backend.models.admission import CapacityError, ResourceClass

    async def main():
        db = DatabaseManager(db_path)
        await db.initialize()
        repo = SQLiteCapacityRepository(db)
        wins = 0
        for i in range(attempts):
            try:
                outcome = await repo.acquire(
                    ResourceClass.WORKFLOW_TASK, "tenant:loadtest", "u1",
                    f"exec-{os.getpid()}-{i}", limit=15)
                wins += 1 if outcome.created else 0
            except CapacityError:
                pass
        await db.close()
        return wins

    return asyncio.run(main())


def _schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            identifier TEXT NOT NULL,
            group_name TEXT NOT NULL,
            window_seconds INTEGER NOT NULL,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            cleanup_after INTEGER NOT NULL,
            policy_version TEXT NOT NULL DEFAULT 'p3.v1',
            PRIMARY KEY (identifier, group_name, window_seconds, window_start)
        );
        """
    )
    conn.commit()
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=50)
    args = parser.parse_args()

    allowance = 25
    tmp = tempfile.mkdtemp(prefix="agentos-loadtest-")
    db_path = str(Path(tmp) / "load.db")
    _schema(db_path)
    window_start = int(time.time()) // 60 * 60

    print(f"Rate limiter: {args.workers} processes x {args.attempts} attempts, "
          f"allowance={allowance}, independent SQLite connections")
    started = time.monotonic()
    with mp.Pool(args.workers) as pool:
        wins = pool.starmap(
            _worker_rate,
            [(db_path, window_start, args.attempts) for _ in range(args.workers)],
        )
    elapsed = time.monotonic() - started
    total_attempts = args.workers * args.attempts
    total_wins = sum(wins)
    print(f"  admitted={total_wins} rejected={total_attempts - total_wins} "
          f"of {total_attempts} attempts in {elapsed:.2f}s")
    ok = total_wins == allowance
    print(f"  EXACTLY {allowance} admitted across all processes: "
          f"{'PASS' if ok else f'FAIL (got {total_wins})'}")

    print(f"\nCapacity leases: {args.workers} processes x {args.attempts} "
          f"distinct executions, cap=15")
    cap_path = str(Path(tmp) / "capacity.db")
    sys.path.insert(0, str(REPO_ROOT))
    # Seed the schema once (capacity table lives in the full DB schema).
    seed = _seed_capacity(cap_path)
    if not seed:
        # Phase 24 §24.3: a skipped mandatory subcheck is a structured
        # FAIL — it can never exit 0 (the audit found this skip path
        # returned success when the rate-limit portion passed).
        print(json.dumps({
            "workload": "admission", "host": os.uname().nodename,
            "stage": "performance", "exit": 1,
            "rate_limit": {"ok": bool(ok), "allowance": allowance,
                           "admitted": total_wins,
                           "accepted_total": total_wins,
                           "attempts": total_attempts},
            "capacity": {"ok": False, "skipped": True,
                         "reason": "capacity schema seed failed"},
            "ok": False}, indent=2))
        return 1
    started = time.monotonic()
    with mp.Pool(args.workers) as pool:
        cap_wins = pool.starmap(
            _worker_capacity,
            [(cap_path, args.attempts) for _ in range(args.workers)],
        )
    elapsed = time.monotonic() - started
    cap_total = sum(cap_wins)
    print(f"  leases_created={cap_total} of {args.workers * args.attempts} "
          f"distinct executions in {elapsed:.2f}s")
    cap_ok = cap_total == 15
    print(f"  EXACTLY 15 leases created across all processes: "
          f"{'PASS' if cap_ok else f'FAIL (got {cap_total})'}")
    # Structured machine-readable outcome: every mandatory subcheck has
    # its own recorded result; the harness parses this file as evidence.
    out_path = args.output or Path(tmp) / "outcome.json"
    out_path.write_text(json.dumps({
        "workload": "admission", "host": os.uname().nodename,
        "stage": "performance", "exit": 0 if (ok and cap_ok) else 1,
        "rate_limit": {"ok": bool(ok), "allowance": allowance,
                       "admitted": total_wins,
                       "accepted_total": total_wins,
                       "attempts": total_attempts},
        "capacity": {"ok": bool(cap_ok), "skipped": False,
                     "leases": cap_total},
        "ok": bool(ok and cap_ok)}, indent=2))
    print(f"  outcome file: {out_path}")
    return 0 if (ok and cap_ok) else 1


def _seed_capacity(db_path: str) -> bool:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import asyncio
        from backend.repositories.sqlite.database import DatabaseManager

        async def seed():
            db = DatabaseManager(db_path)
            await db.initialize()
            await db.close()

        asyncio.run(seed())
        return True
    except Exception as exc:
        print(f"  seed failed: {exc}")
        return False


if __name__ == "__main__":
    raise SystemExit(main())
