"""Per-runner-volume execution journal, also used by the Docker supervisor thread."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class ExecutionStore:
    def __init__(self, path):
        self.path = Path(path)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS managed_executions (
                id TEXT PRIMARY KEY, request TEXT NOT NULL, state TEXT NOT NULL,
                cleanup TEXT NOT NULL, deadline REAL NOT NULL, result TEXT,
                reason TEXT, stopped REAL)""")
            # §28.2: a journal written by an older build is carried
            # forward in place — records survive the restart instead of
            # every write failing on a missing column.
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(managed_executions)")}
            for name, decl in (("result", "TEXT"), ("reason", "TEXT"),
                               ("stopped", "REAL")):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE managed_executions ADD COLUMN {name} {decl}")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def admit(self, execution_id, request, deadline):
        with closing(self.connect()) as db, db:
            db.execute("""INSERT INTO managed_executions
                (id,request,state,cleanup,deadline) VALUES (?,?,'queued','pending',?)""",
                (execution_id, json.dumps(request), deadline))

    def transition(self, execution_id, before, after):
        slots = ",".join("?" for _ in before)
        with closing(self.connect()) as db, db:
            return db.execute(f"UPDATE managed_executions SET state=? WHERE id=? AND state IN ({slots})",
                              (after, execution_id, *before)).rowcount == 1

    def finish(self, execution_id, state, cleanup, reason):
        with closing(self.connect()) as db, db:
            db.execute("""UPDATE managed_executions SET
                state=CASE WHEN state='cancel_requested' AND :state='completed'
                           THEN 'cancelled' ELSE :state END,
                reason=CASE WHEN state='cancel_requested' AND :state='completed'
                            THEN 'cancelled before result commit' ELSE :reason END,
                cleanup=:cleanup, stopped=:stopped
                WHERE id=:id AND state IN ('queued','running','cancel_requested')""",
                {"id": execution_id, "state": state, "cleanup": cleanup,
                 "reason": reason, "stopped": time.time()})

    def cancel_queued(self, execution_id):
        with closing(self.connect()) as db, db:
            return db.execute("""UPDATE managed_executions
                SET state='cancelled', cleanup='cleaned', stopped=?, reason='cancelled before launch'
                WHERE id=? AND state='queued'""", (time.time(), execution_id)).rowcount == 1

    def put(self, execution_id, result):
        with closing(self.connect()) as db, db:
            if db.execute("UPDATE managed_executions SET result=? WHERE id=? "
                          "AND state IN ('queued','running','cancel_requested')",
                          (json.dumps(result), execution_id)).rowcount != 1:
                raise ValueError("execution was not admitted or is already terminal")

    def get(self, execution_id):
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM managed_executions WHERE id=?", (execution_id,)).fetchone()
            return dict(row) if row else None

    def cancellation_requested(self, execution_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT state FROM managed_executions WHERE id=?", (execution_id,)).fetchone()
            return row is not None and row[0] == "cancel_requested"

    def expired(self):
        with closing(self.connect()) as db:
            return [r[0] for r in db.execute("""SELECT id FROM managed_executions
                WHERE deadline < ? AND (state IN ('running','queued','cancel_requested')
                OR cleanup='orphaned')""", (time.time(),))]

    def cleaned(self, execution_id):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE managed_executions SET cleanup='cleaned' WHERE id=?", (execution_id,))
