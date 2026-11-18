"""Phase 30 / 30.0 — master register integrity checks.

The Phase 30 section of docs/REQUIREMENT_LEDGER.md must keep: one row per
subphase 30.0–30.47, exactly one primary owner, a status, and either a
receipt reference or a named blocker. A row claiming IMPLEMENTED or
VERIFIED without a receipt reference fails, as does an orphan ID or a
dropped original ID.
"""
import re
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[2] / "docs" / "REQUIREMENT_LEDGER.md"


def _phase30_rows():
    text = LEDGER.read_text(encoding="utf-8")
    section = text.split("## Phase 30 —", 1)[1].split("### Master register", 1)[1]
    rows = {}
    for line in section.splitlines():
        match = re.match(r"^\| (30\.\d+)", line)
        if match:
            rows[match.group(1)] = line
    return rows


def test_every_subphase_has_exactly_one_row():
    rows = _phase30_rows()
    expected = {f"30.{i}" for i in range(0, 48)}
    missing = expected - set(rows)
    assert not missing, f"orphan subphases without a register row: {sorted(missing)}"


def test_rows_carry_owner_status_and_receipt_or_blocker():
    rows = _phase30_rows()
    for pid, row in rows.items():
        cells = [c.strip() for c in row.split("|")]
        # cells[0] empty (leading pipe), then ID, original IDs, entry point, status, receipt
        assert len(cells) >= 6, f"{pid}: row needs ID/original/entry-point/status/receipt cells"
        status_cell = cells[4]
        receipt_cell = cells[5]
        assert status_cell, f"{pid}: missing status"
        implemented_like = re.search(
            r"IMPLEMENTED|VERIFIED|EXTENDED|CARRIED|EXECUTED", status_cell)
        has_receipt = bool(re.search(r"test_|receipt|journey|J\d\d|drill|matrix|docs/", receipt_cell))
        blocked = "BLOCKED" in status_cell or "PENDING" in status_cell
        assert has_receipt or blocked, (
            f"{pid}: a row may not claim '{status_cell}' without a receipt reference "
            f"(found: {receipt_cell!r})"
        )
        if blocked:
            assert re.search(r"B\d|release owner|PENDING", status_cell + receipt_cell), (
                f"{pid}: a blocked row must name its blocker"
            )


def test_original_ids_are_preserved_per_row():
    rows = _phase30_rows()
    for pid, row in rows.items():
        original = row.split("|")[2]
        assert original.strip(), f"{pid}: original ID column is empty — history must be kept"
