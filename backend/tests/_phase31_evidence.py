"""Phase 31 §31.2 — shared fixture helpers for evidence-gate tests.

The verdict now requires:
  - the three journey-independent scenarios (ORDINARY-CHAT,
    LINK-PREVIEW, DOCK-RESOURCE-IDENTITY) to carry their own
    receipt-bound records, and
  - every journey record's run ID to be named by retained product
    bytes (an observation file, a scenario receipt, or a product
    receipt among the evidence).

These helpers build GENUINE fixture records (real receipts with hashes
and mandatory assertions) so each named regression exercises exactly
one breakage at a time.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scenario_receipts import (  # noqa: E402
    RECEIPT_SCHEMA, catalog_scenario,
)

CAND = "a" * 64
TARGET = "local-durable-postgres"
DIGEST = "d" * 64
OPERATOR_ATTEMPT = "p31-operator-attempt-1"


def write_scenario_receipt(ev_dir: Path, scenario_id: str, run_id: str,
                           *, candidate: str = CAND, target: str = TARGET,
                           attempt: str = OPERATOR_ATTEMPT,
                           status: str = "passed",
                           assertions_override: list | None = None,
                           journey_id: str | None = "SENTINEL") -> Path:
    """Write one schema-valid scenario receipt. Returns its path."""
    entry = catalog_scenario(scenario_id)
    assert entry, f"fixture used unknown scenario {scenario_id}"
    if journey_id == "SENTINEL":
        journey_id = entry.get("journey_id")
    mandatory = entry.get("mandatory_assertions") or []
    assertions = assertions_override if assertions_override is not None else [
        {"id": aid, "expected": entry["pass_condition"],
         "observed": "independent observer value", "ok": status == "passed"}
        for aid in mandatory]
    evidence_path = ev_dir / f"receipt-evidence-{scenario_id}.json"
    evidence_path.write_text(json.dumps(
        {"run_id": run_id, "scenario_id": scenario_id,
         "observation": "independent"}, indent=2))
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "scenario_id": scenario_id,
        "scenario_version": entry["version"],
        "fixture_version": f"{scenario_id.lower()}-fixture@1",
        "journey_id": journey_id,
        "candidate_sha256": candidate,
        "config_sha256": "c" * 64,
        "deployment_image_digests_sha256": DIGEST,
        "target": target,
        "operator_attempt_id": attempt,
        "actor": {"kind": "tenant-user", "tenant_ref": "tenant-a",
                  "user_ref": "u-1"},
        "input": {"goal": f"exercise {scenario_id}",
                  "sanitized_digest": "e" * 64},
        "resources": {"tenants": ["tenant-a"], "destination": "ledger-1"},
        "actions": [{"at": "2026-11-14T00:00:00Z",
                     "action": "run_scenario", "detail": scenario_id}],
        "criterion": entry["pass_condition"],
        "observation": {"kind": "independent", "run_id": run_id,
                        "detail": f"observed {run_id} externally"},
        "assertions": assertions,
        "relations": {"run_ids": [run_id] if status == "passed" else []},
        "evidence": [{"result_file": str(evidence_path),
                      "result_sha256": hashlib.sha256(
                          evidence_path.read_bytes()).hexdigest()}],
        "cleanup": {"ok": status == "passed", "detail": "fixtures removed"},
        "status": status,
        "ok": status == "passed",
        "started_at": "2026-11-14T00:00:00Z",
        "ended_at": "2026-11-14T00:01:00Z",
    }
    scenarios_dir = ev_dir / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    path = scenarios_dir / f"{scenario_id}@{entry['version']}--{attempt}.json"
    path.write_text(json.dumps(receipt, indent=2))
    return path


def scenario_record(ev_dir: Path, scenario_id: str, run_id: str,
                    *, candidate: str = CAND, target: str = TARGET,
                    attempt: str = OPERATOR_ATTEMPT) -> dict:
    """A journey-independent scenario record bound to its receipt."""
    path = write_scenario_receipt(ev_dir, scenario_id, run_id,
                                  candidate=candidate, target=target,
                                  attempt=attempt, journey_id=None)
    entry = catalog_scenario(scenario_id)
    return {
        "scenario_id": scenario_id,
        "journey_id": None,
        "ok": True,
        "status": "passed",
        "production_action": f"scenario {scenario_id}: executed against "
                             f"the live stack on operator attempt {attempt}",
        "run_id": run_id,
        "expected_postcondition": entry["pass_condition"],
        "observed_result": f"independently observed {run_id}",
        "candidate_sha256": candidate,
        "target_id": target,
        "target_image_digests_sha256": DIGEST,
        "attempt_id": attempt,
        "scenario_receipts": [{
            "scenario_id": scenario_id,
            "scenario_version": entry["version"],
            "result_file": str(path),
            "result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "operator_attempt_id": attempt,
        }],
        "evidence": [],
    }


def scenario_section(ev_dir: Path, *, candidate: str = CAND,
                     target: str = TARGET,
                     attempt: str = OPERATOR_ATTEMPT) -> dict:
    """The mandatory journey-independent scenarios, each with a passing
    receipt — the genuine-packet baseline."""
    return {
        sid: scenario_record(ev_dir, sid, f"run-{sid.lower()}",
                             candidate=candidate, target=target,
                             attempt=attempt)
        for sid in ("ORDINARY-CHAT", "LINK-PREVIEW",
                    "DOCK-RESOURCE-IDENTITY")
    }


def journey_evidence_file(ev_dir: Path, jid: str) -> Path:
    """A journey's retained observation bytes naming its real run —
    the provenance path (c) a direct evidence entry provides."""
    path = ev_dir / f"{jid}.json"
    path.write_text(json.dumps(
        {"journey": jid, "run_id": f"run-{jid}", "observed": True}))
    return path
