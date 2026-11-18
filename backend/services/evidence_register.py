"""Evidence register (Phase 14 §14.0) — the acceptance claims for every
subphase live as QUERYABLE rows, each bound to the test that proves it.

Rules:
- A row claims a subphase with a status and an evidence level.
- `verified_locally` (and any stronger level) REQUIRES a test reference —
  the register refuses unevidenced claims by construction.
- One row per subphase: re-recording UPDATES in place (with a new
  evidence trail in the audit columns), never duplicates.
- The register is read by the release-evidence assembler (§14.38): a
  phase's release claim enumerates its register rows, so "done" is
  always a query, never an assertion.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

EVIDENCE_LEVELS = ("verified_locally", "integrated", "blocked", "excluded")
# Levels that PROVE behavior — they must carry the test that ran.
LEVELS_REQUIRING_TESTS = ("verified_locally", "integrated")


class EvidenceRegisterError(Exception):
    pass


class EvidenceRegister:
    def __init__(self, db):
        self.db = db

    async def record(self, *, subphase_id: str, claim: str,
                     status: str, test_ref: Optional[str] = None,
                     evidence_level: str = "verified_locally",
                     recorded_by: str = "acceptance") -> Dict[str, Any]:
        """Record or update one subphase's evidence. A proving level
        without a test reference is refused (§14.0: the register cannot
        hold unevidenced claims)."""
        if evidence_level not in EVIDENCE_LEVELS:
            raise EvidenceRegisterError(
                f"Unknown evidence level: {evidence_level}")
        if evidence_level in LEVELS_REQUIRING_TESTS and not (test_ref or "").strip():
            raise EvidenceRegisterError(
                f"Subphase {subphase_id}: evidence level "
                f"'{evidence_level}' requires a test reference — the "
                "register refuses unevidenced claims")
        now = epoch_now()
        await self.db.execute(
            """INSERT INTO evidence_register
               (subphase_id, claim, status, test_ref, evidence_level,
                recorded_by, recorded_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(subphase_id) DO UPDATE SET
                 claim=excluded.claim, status=excluded.status,
                 test_ref=excluded.test_ref,
                 evidence_level=excluded.evidence_level,
                 recorded_by=excluded.recorded_by,
                 updated_at=excluded.updated_at""",
            (subphase_id, claim, status, test_ref, evidence_level,
             recorded_by, now, now))
        await self.db.commit()
        return await self.get(subphase_id)

    async def get(self, subphase_id: str) -> Optional[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM evidence_register WHERE subphase_id=?",
            (subphase_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_all(self) -> List[Dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM evidence_register ORDER BY subphase_id")
        return [dict(r) for r in await cursor.fetchall()]

    async def summary(self) -> Dict[str, Any]:
        """The release-assembler's view: counts by level, and the rows
        that still block a release claim."""
        rows = await self.list_all()
        by_level: Dict[str, int] = {}
        blocking = []
        for r in rows:
            by_level[r["evidence_level"]] = \
                by_level.get(r["evidence_level"], 0) + 1
            if r["evidence_level"] in ("blocked", "excluded"):
                blocking.append(r["subphase_id"])
        return {"total": len(rows), "by_level": by_level,
                "blocking": sorted(blocking)}


_register: Optional[EvidenceRegister] = None


def set_evidence_register(repo) -> None:
    global _register
    _register = repo


def get_evidence_register() -> Optional[EvidenceRegister]:
    return _register
