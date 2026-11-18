"""Phase 14 §14.38: release evidence is ASSEMBLED from queryable
registers — the release claim enumerates the evidence register's rows,
and every register row for a proving level carries the test that ran.
"Done" is a query, never an assertion."""
import pytest
import pytest_asyncio

from backend.repositories.sqlite.database import DatabaseManager

PHASE14_SUBPHASES = [f"14.{i}" for i in range(1, 40)]


@pytest_asyncio.fixture
async def reg(tmp_path):
    db = DatabaseManager(str(tmp_path / "release.db"))
    await db.initialize()
    from backend.services.evidence_register import EvidenceRegister
    r = EvidenceRegister(db)
    # Record EVERY Phase 14 subphase with its proving test.
    for i in PHASE14_SUBPHASES:
        await r.record(
            subphase_id=i,
            claim=f"Phase 14 subphase {i} acceptance",
            status="complete",
            test_ref="test_phase14_flagship.py::test_the_complete_phase14_vertical")
    try:
        yield r
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_release_claim_enumerates_every_subphase(reg):
    """The assembler's query: every subphase has a row; none block."""
    summary = await reg.summary()
    assert summary["total"] == len(PHASE14_SUBPHASES)
    assert summary["blocking"] == [], "nothing blocks the phase-14 claim"


@pytest.mark.asyncio
async def test_every_proving_row_names_its_test(reg):
    rows = await reg.list_all()
    assert rows
    for row in rows:
        if row["evidence_level"] in ("verified_locally", "integrated"):
            assert (row["test_ref"] or "").strip(), (
                f"{row['subphase_id']} claims {row['evidence_level']} "
                "without a test reference")


@pytest.mark.asyncio
async def test_a_blocked_subphase_is_visible_to_the_assembler(reg):
    await reg.record(subphase_id="14.99", claim="staging drill",
                     status="pending", evidence_level="blocked")
    summary = await reg.summary()
    assert "14.99" in summary["blocking"], (
        "blocked work is visible in the release claim, never hidden")
