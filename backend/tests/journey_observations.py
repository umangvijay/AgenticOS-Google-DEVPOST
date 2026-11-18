"""§27.1 journey observation receipts (audit 2026-11-13).

A journey's evidence must carry REAL product observations — run/build/
artifact IDs and the externally observed values — not templated text.
When the qualification harness sets JOURNEY_OBSERVATION_DIR, a journey
test emits its observed receipt there; the evidence generator consumes
it instead of synthesizing run IDs. Without the env var (ordinary test
runs) emission is a no-op.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def emit(journey_id: str, observation: dict) -> str | None:
    """Persist one journey's observed product receipt. Returns the path
    or None when the harness did not request observations."""
    out_dir = os.environ.get("JOURNEY_OBSERVATION_DIR")
    if not out_dir:
        return None
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{journey_id.lower()}-observation.json"
    path.write_text(json.dumps(
        {"journey_id": journey_id, **observation},
        indent=2, default=str))
    return str(path)
