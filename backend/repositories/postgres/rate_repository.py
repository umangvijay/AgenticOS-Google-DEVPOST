"""Atomic shared admission for API and worker replicas on PostgreSQL."""
import time

from backend.security.shared_rate_limiter import (
    SharedAdmitResult, _validate_admission_inputs, validate_identity,
)


class PostgresSharedRateRepository:
    def __init__(self, db):
        self.db = db

    async def admit(self, identifier, group, window_start, max_requests,
                    window_seconds=60, policy_version="p3.v1"):
        identifier = validate_identity(identifier)
        _validate_admission_inputs(max_requests, window_seconds)
        end = int(window_start) + int(window_seconds)
        if int(max_requests) == 0:
            return SharedAdmitResult(False, 0, int(window_start), end)
        row = await self.db.fetch_one(
            """INSERT INTO pg_rate_limits
               (identifier, group_name, window_seconds, window_start,
                window_end, count, cleanup_after, policy_version)
               VALUES ($1,$2,$3,$4,$5,1,$6,$7)
               ON CONFLICT (identifier, group_name, window_seconds, window_start)
               DO UPDATE SET count = pg_rate_limits.count + 1
               WHERE pg_rate_limits.count < $8 RETURNING count""",
            identifier, group, int(window_seconds), int(window_start), end,
            end + int(window_seconds), policy_version, int(max_requests))
        return SharedAdmitResult(row is not None, int(row["count"]) if row else int(max_requests),
                                 int(window_start), end)

    async def purge_expired(self, older_than_epoch):
        cutoff = min(int(older_than_epoch), int(time.time()))
        rows = await self.db.fetch_all(
            """DELETE FROM pg_rate_limits WHERE
               (identifier, group_name, window_seconds, window_start) IN
               (SELECT identifier, group_name, window_seconds, window_start
                FROM pg_rate_limits WHERE cleanup_after < $1 LIMIT 500)
               RETURNING identifier""", cutoff)
        return len(rows)
