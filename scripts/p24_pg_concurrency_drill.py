#!/usr/bin/env python3
"""Phase 24 §24.4 — multi-process Postgres concurrency drill.

1. Contested shared rate limiting: N independent processes race one
   identifier; the accepted total must EXACTLY match the durable
   pg_rate_limits ledger row and the declared allowance.
2. Contested budget reservations: two processes reserve concurrently;
   accepted holds must match the ledger.

Outcome: structured JSON to stdout; exit 0 only when every check holds.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DSN = "postgresql://agentos:agentos@127.0.0.1:55432/agentos_test"
ALLOWANCE = 12
WINDOW = int(time.time()) // 60 * 60
OUTCOMES: dict = {"rate_limit": None, "budget": None, "ok": True}


def rate_racer(args):
    user_id, window = args
    """Runs in a Pool worker; returns the win count (or an error string)."""
    async def run():
        from backend.repositories.postgres.database import PostgresDatabaseManager
        from backend.repositories.postgres.rate_repository import (
            PostgresSharedRateRepository)
        mgr = PostgresDatabaseManager(DSN)
        await mgr.initialize()
        repo = PostgresSharedRateRepository(mgr)
        wins = 0
        for _ in range(30):
            res = await repo.admit(user_id, "feedback", window,
                                   ALLOWANCE, window_seconds=60)
            wins += 1 if res.allowed else 0
        await mgr.close()
        return wins
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"


def budget_racer(uid):
    """Runs in a Pool worker; returns the reservation id (or error)."""
    async def run():
        from backend.repositories.postgres.database import PostgresDatabaseManager
        from backend.repositories.postgres.repositories import PostgresBudgetRepository
        from backend.models.usage import ReservationRequest, BudgetScope
        from backend.services.budget_service import BucketLimit
        mgr = PostgresDatabaseManager(DSN)
        await mgr.initialize()
        repo = PostgresBudgetRepository(mgr)
        from backend.models.usage import BudgetError, BudgetErrorCode
        buckets = [BucketLimit(BudgetScope.USER, uid, 1000)]
        try:
            res = await repo.reserve(ReservationRequest(user_id=uid, units=500, provider="ollama",
                                                        model="m", purpose="generation"), buckets)
            return res.reservation_id
        except BudgetError as exc:
            if exc.code != BudgetErrorCode.BUDGET_EXHAUSTED:
                raise
            return "denied"
        finally:
            await mgr.close()
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {type(exc).__name__}: {exc}"


def main() -> int:
    import multiprocessing as mp
    import uuid
    user_id = "drill-" + uuid.uuid4().hex
    with mp.Pool(6) as pool:
        wins = pool.map(rate_racer, [(user_id, WINDOW)] * 6)
    with mp.Pool(4) as pool:
        ids = pool.map(budget_racer, [user_id] * 4)
    errors = [w for w in wins if isinstance(w, str)]
    total = sum(w for w in wins if isinstance(w, int))

    async def ledger_check():
        from backend.repositories.postgres.database import PostgresDatabaseManager
        m = PostgresDatabaseManager(DSN)
        await m.initialize()
        row = await m.fetch_one(
            "SELECT count FROM pg_rate_limits WHERE identifier=$1 "
            "AND group_name='feedback' AND window_start=$2",
            user_id, WINDOW)
        ledger = int(row["count"]) if row else 0
        # Budget: each racer's reservation must exist in the ledger.
        found = 0
        for r in ids:
            if isinstance(r, str) and not r.startswith("ERROR"):
                row = await m.fetch_one(
                    "SELECT 1 AS x FROM budget_reservations "
                    "WHERE reservation_id=$1", r)
                found += 1 if row else 0
        held = await m.fetch_one("SELECT reserved FROM budget_buckets WHERE scope='user' AND subject_id=$1", user_id)
        await m.close()
        return ledger, found, held["reserved"] if held else 0

    ledger, found, held = asyncio.run(ledger_check())

    rate_ok = (not errors and total == ALLOWANCE and ledger == ALLOWANCE)
    budget_ok = found == 2 and ids.count("denied") == 2 and held == 1000
    OUTCOMES["rate_limit"] = {
        "ok": rate_ok, "processes": 6, "attempts_each": 30,
        "allowance": ALLOWANCE, "accepted_total": total,
        "ledger_count": ledger, "errors": errors}
    OUTCOMES["budget"] = {"ok": budget_ok, "reservations_found": found,
                          "denied": ids.count("denied"), "held": held, "limit": 1000}
    OUTCOMES["ok"] = rate_ok and budget_ok
    print(json.dumps(OUTCOMES, indent=2))
    return 0 if OUTCOMES["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
