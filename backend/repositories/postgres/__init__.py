"""PostgreSQL repository layer (Phase 20 §20.4).

Core durable subset on Postgres 16: users, workflows/runs, tasks,
commands, events, deliverables, and budget reservations, with EXPLICIT
versioned migrations (``migrations/*.sql`` applied in order under a
schema_migrations ledger). The contract suite lives in
``backend/tests/test_phase20_postgres.py`` and runs against any
reachable Postgres (compose profile ``core`` starts one on :55432).
"""
from backend.repositories.postgres.database import PostgresDatabaseManager

__all__ = ["PostgresDatabaseManager"]
