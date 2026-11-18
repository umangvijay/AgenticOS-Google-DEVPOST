"""Phase 31 §31.2 — the versioned scenario-receipt contract.

A scenario receipt is the record ONE scenario execution leaves behind:
the operator attempt that ran it, the candidate/target it is bound to,
the real product resources it created, the timestamped actions, the
expected criterion, the INDEPENDENTLY observed value, per-assertion
results (each screenshot backed by an assertion), evidence hashes, and
the cleanup result.

The producer (`scripts/generate_journey_evidence.py`) consumes these
receipts to compose journey verdicts; the consumer
(`scripts/qualify_release.py::_journey_problems` /
`_scenario_problems`) re-validates them. Both use this module so the
contract cannot drift between the two sides.

Negative checks (each must fail specifically):
  - duplicate JSON keys (parsed with unique_object)
  - altered receipt bytes (hash mismatch via verify_result_file)
  - a receipt bound to a different journey/scenario than where it is
    attached (wrong-journey evidence)
  - a receipt from a previous operator attempt or different
    candidate/target/deployment (foreign evidence)
  - a receipt whose status is passed but which skipped a mandatory
    subcheck (assertion) declared in the catalog
  - a screenshot evidence entry with no corresponding assertion
  - a passing receipt whose cleanup failed or never ran
  - a passing receipt whose run relations contain no real product
    record (composed/generated IDs only)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "docs" / "qualification" / "phase-31-scenarios.json"

# The receipt schema version. A receipt whose schema differs is a
# foreign generation and is rejected rather than loosely interpreted.
RECEIPT_SCHEMA = "phase31-scenario-receipt/1"

# Statuses a receipt may record. `passed` is the only passing status;
# everything else keeps the journey/scenario open and retains its
# observations up to the interruption point.
PASSING_STATUS = "passed"
KNOWN_STATUSES = (
    "passed",              # all mandatory assertions observed ok + cleanup ok
    "failed_assertion",    # a mandatory assertion observed a wrong value
    "setup_failed",        # stopped while provisioning resources
    "cancelled",           # operator cancelled mid-run
    "blocked_external",    # a named external prerequisite is missing
    "unobserved",          # a required observation could not be made
)

REQUIRED_FIELDS = (
    "schema", "scenario_id", "scenario_version", "fixture_version",
    "journey_id", "candidate_sha256", "config_sha256",
    "deployment_image_digests_sha256", "target", "operator_attempt_id",
    "actor", "input", "resources", "actions", "criterion",
    "observation", "assertions", "relations", "evidence", "cleanup",
    "status", "started_at", "ended_at",
)


class ReceiptError(ValueError):
    """Raised for structurally unreadable receipts (duplicate keys,
    non-object roots, bad encodings)."""


def unique_object(pairs):
    """json object_pairs_hook that REJECTS duplicate keys — a receipt
    with repeated keys is malformed evidence, not a last-write-wins."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_receipt(path: Path) -> dict:
    """Parse a receipt file with duplicate-key rejection."""
    try:
        receipt = json.loads(Path(path).read_text(),
                             object_pairs_hook=unique_object)
    except ReceiptError:
        raise
    except ValueError as exc:
        raise ReceiptError(f"unreadable receipt JSON: {exc}") from exc
    if not isinstance(receipt, dict):
        raise ReceiptError("receipt root must be a JSON object")
    return receipt


def load_catalog() -> dict:
    """The versioned scenario catalog (scenario IDs → journeys,
    mandatory assertions, fixtures, faults)."""
    data = json.loads(CATALOG_PATH.read_text())
    if not isinstance(data, dict) or "scenarios" not in data:
        raise ReceiptError("scenario catalog is malformed")
    return data


_CATALOG: dict | None = None


def catalog() -> dict:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = load_catalog()
    return _CATALOG


def catalog_scenario(scenario_id: str) -> dict | None:
    for entry in catalog().get("scenarios") or []:
        if entry.get("scenario_id") == scenario_id:
            return entry
    return None


def catalog_by_journey(journey_id: str) -> list[dict]:
    return [s for s in catalog().get("scenarios") or []
            if s.get("journey_id") == journey_id]


def mandatory_scenarios() -> list[dict]:
    return [s for s in catalog().get("scenarios") or []
            if s.get("mandatory") and not s.get("journey_id")]


def receipt_scenario(receipt: dict) -> dict | None:
    return catalog_scenario(str(receipt.get("scenario_id") or ""))


# Composed run references the producer writes when NO product run was
# observed. They document why a journey is open — they are not product
# records and can never back a passing journey.
_COMPOSED_RUN_RE = re.compile(
    r"^J\d+:(unobserved|stage:[0-9a-f]+)$|^journey-")


def is_composed_run_reference(run_id: str) -> bool:
    return bool(_COMPOSED_RUN_RE.match(str(run_id or "")))


def real_run_ids(receipt: dict) -> list[str]:
    """The REAL product record IDs a receipt observed: workflow runs
    (relations.run_ids / observation) plus build/revision/artifact/
    execution/session records for journeys whose product path produces
    those instead of a workflow run (install, forge, browser). Composed
    references are excluded."""
    relations = receipt.get("relations") or {}
    runs: list[str] = []
    for key in ("run_ids", "build_ids", "artifact_ids", "execution_ids",
                "session_ids", "revisions"):
        for rid in relations.get(key) or []:
            rid = str(rid or "").strip()
            if rid and not is_composed_run_reference(rid) and rid not in runs:
                runs.append(rid)
    obs = receipt.get("observation") or {}
    if isinstance(obs, dict):
        for key in ("run_id", "workflow_run_id", "build_id", "artifact_id",
                    "execution_id", "session_id", "active_revision_id"):
            rid = str(obs.get(key) or "").strip()
            if rid and not is_composed_run_reference(rid) and rid not in runs:
                runs.append(rid)
    return runs


def validate_receipt(
    receipt: dict,
    *,
    expected_scenario_id: str | None = None,
    expected_journey_id: str | None = None,
    candidate_sha256: str | None = None,
    target_id: str | None = None,
    deployment_digest: str | None = None,
    operator_attempt_id: str | None = None,
) -> list[str]:
    """Validate ONE parsed receipt against the contract and the
    supplied bindings. Returns a list of specific problems (empty =
    structurally valid)."""
    problems: list[str] = []
    scenario_id = str(receipt.get("scenario_id") or "")
    if not scenario_id:
        return ["receipt has no scenario_id"]

    # ── structure ────────────────────────────────────────────────────
    if receipt.get("schema") != RECEIPT_SCHEMA:
        problems.append(
            f"receipt schema {receipt.get('schema')!r} is not "
            f"{RECEIPT_SCHEMA!r} — a foreign generation")
    entry = receipt_scenario(receipt)
    if entry is None:
        problems.append(f"scenario {scenario_id} is not in the catalog")
        return problems
    missing = [f for f in REQUIRED_FIELDS if receipt.get(f) in (None, "")]
    # actor/resources/relations may legitimately be empty dicts; only
    # flag genuinely absent or empty-string required scalars.
    missing = [f for f in missing
               if not isinstance(receipt.get(f), (dict, list))]
    # journey_id is null for journey-independent scenarios (the three
    # additional Phase 31 scenarios); its absence there is the contract.
    if not entry.get("journey_id") and "journey_id" in missing:
        missing.remove("journey_id")
    if missing:
        problems.append(
            f"receipt missing required fields: {', '.join(sorted(missing))}")

    # ── binding to where the receipt is attached ─────────────────────
    if expected_scenario_id and scenario_id != expected_scenario_id:
        problems.append(
            f"receipt for {scenario_id} attached as {expected_scenario_id} "
            "— wrong-scenario evidence")
    jid = receipt.get("journey_id")
    if expected_journey_id and jid != expected_journey_id:
        problems.append(
            f"receipt journey {jid!r} does not match expected "
            f"{expected_journey_id!r} — wrong-journey evidence")
    elif (not expected_journey_id and jid
          and not entry.get("journey_id")):
        problems.append(
            f"receipt names journey {jid!r} but the scenario "
            f"{scenario_id} is journey-independent")
    if candidate_sha256 and receipt.get("candidate_sha256") != candidate_sha256:
        problems.append(
            f"receipt candidate {str(receipt.get('candidate_sha256'))[:12]}… "
            "differs from this attempt's candidate — foreign evidence")
    if target_id and str(receipt.get("target")) != str(target_id):
        problems.append(
            f"receipt target {receipt.get('target')!r} differs from "
            f"{target_id!r} — foreign evidence")
    if (deployment_digest and receipt.get("deployment_image_digests_sha256")
            and receipt["deployment_image_digests_sha256"] != deployment_digest):
        problems.append(
            "receipt deployment digest differs from this target's "
            "image digests — foreign evidence")
    if (operator_attempt_id and receipt.get("operator_attempt_id")
            and str(receipt["operator_attempt_id"]) != str(operator_attempt_id)):
        problems.append(
            f"receipt from operator attempt "
            f"{receipt['operator_attempt_id']!r} — this verdict consumes "
            f"attempt {operator_attempt_id!r} only")

    # ── status, assertions, cleanup ──────────────────────────────────
    status = receipt.get("status")
    if status not in KNOWN_STATUSES:
        problems.append(f"receipt status {status!r} is not a known outcome")
    assertions = receipt.get("assertions") or []
    if not isinstance(assertions, list) or not assertions:
        problems.append("receipt records no assertions — an unasserted "
                        "observation is not evidence")
        return problems
    seen_ids: set[str] = set()
    for a in assertions:
        if not isinstance(a, dict) or not str(a.get("id") or "").strip():
            problems.append("assertion record without an id")
            continue
        aid = str(a["id"])
        if aid in seen_ids:
            problems.append(f"duplicate assertion id {aid}")
        seen_ids.add(aid)
        if a.get("ok") is not True and "observed" not in a:
            problems.append(
                f"assertion {aid} records no observed value — a failed "
                "assertion must retain what was observed")
        shot = a.get("screenshot")
        if shot is not None and not str(shot).strip():
            problems.append(f"assertion {aid} has an empty screenshot ref")
        if a.get("ok") is True and not str(a.get("expected") or "").strip():
            problems.append(f"assertion {aid} passed without an expected "
                            "criterion")
    # Mandatory subchecks: a passed receipt must have observed EVERY
    # assertion the catalog declares mandatory for this scenario version.
    mandatory = entry.get("mandatory_assertions") or []
    if status == PASSING_STATUS:
        skipped = [m for m in mandatory if m not in seen_ids]
        if skipped:
            problems.append(
                f"receipt claims {PASSING_STATUS} but skipped mandatory "
                f"subchecks: {', '.join(skipped)}")
        failed = [str(a["id"]) for a in assertions
                  if isinstance(a, dict) and a.get("ok") is not True]
        if failed:
            problems.append(
                f"receipt claims {PASSING_STATUS} with failed assertions: "
                f"{', '.join(failed)}")
        cleanup = receipt.get("cleanup") or {}
        if not isinstance(cleanup, dict) or cleanup.get("ok") is not True:
            problems.append(
                f"receipt claims {PASSING_STATUS} but cleanup did not "
                "succeed — leftover resources are not a passing outcome")
        if not real_run_ids(receipt):
            problems.append(
                f"receipt claims {PASSING_STATUS} with no real product run "
                "record — composed or generated IDs cannot establish the "
                "postcondition")
    # Version: the catalog owns scenario versions; a receipt written
    # against a different version is a different experiment.
    if int(receipt.get("scenario_version") or 0) != int(entry["version"]):
        problems.append(
            f"receipt scenario_version {receipt.get('scenario_version')} "
            f"!= catalog version {entry['version']} for {scenario_id}")
    # Screenshots must be backed by an assertion.
    for ev in receipt.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("kind") == "screenshot":
            if str(ev.get("assertion_id") or "") not in seen_ids:
                problems.append(
                    f"screenshot evidence {ev.get('result_file')!r} has no "
                    "corresponding assertion")
    return problems


def receipt_passes(receipt: dict) -> bool:
    return (receipt.get("status") == PASSING_STATUS
            and receipt.get("ok") is True)


def evidence_hash_problems(
    receipt: dict, evidence_root: Path,
    verify_result_file,  # callable(stage_label, entry, root) -> list[str]
) -> list[str]:
    """Hash-verify every evidence entry through the consumer's own
    verifier (exists, inside the evidence root, sha256 matches)."""
    problems: list[str] = []
    for i, ev in enumerate(receipt.get("evidence") or []):
        if not isinstance(ev, dict):
            problems.append(f"evidence entry {i} is not a record")
            continue
        label = f"receipt {receipt.get('scenario_id')}/evidence[{i}]"
        if ev.get("kind") == "screenshot":
            # Screenshot files are binary evidence; verify existence +
            # hash without the JSON parsing the shared verifier does.
            p = Path(str(ev.get("result_file") or ""))
            if not p.is_absolute():
                p = ROOT / p
            try:
                p.resolve().relative_to(Path(evidence_root).resolve())
            except ValueError:
                problems.append(f"{label}: screenshot outside the evidence "
                                f"location ({ev.get('result_file')})")
                continue
            import hashlib
            if not p.is_file():
                problems.append(f"{label}: screenshot file does not exist")
            elif str(ev.get("result_sha256") or "").lower() != \
                    hashlib.sha256(p.read_bytes()).hexdigest():
                problems.append(f"{label}: screenshot bytes changed")
        else:
            problems.extend(verify_result_file(label, ev, evidence_root))
    return problems


def main() -> int:  # pragma: no cover - self-check utility
    """Validate the catalog itself and any receipts passed as paths."""
    try:
        cat = catalog()
    except (OSError, ReceiptError, ValueError) as exc:
        print(f"catalog unreadable: {exc}")
        return 1
    ids = [s.get("scenario_id") for s in cat["scenarios"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"catalog has duplicate scenario ids: {sorted(dupes)}")
        return 1
    journeys = [s.get("journey_id") for s in cat["scenarios"] if s.get("journey_id")]
    expected_journeys = {f"J{i:02d}" for i in range(1, 25)}
    if {j for j in journeys if j} - expected_journeys:
        print(f"catalog binds unknown journey ids: "
              f"{sorted({j for j in journeys if j} - expected_journeys)}")
        return 1
    missing = expected_journeys - {j for j in journeys if j}
    if missing:
        print(f"catalog is missing journeys: {sorted(missing)}")
        return 1
    problems: list[str] = []
    for path in sys.argv[1:]:
        try:
            receipt = load_receipt(Path(path))
        except ReceiptError as exc:
            problems.append(f"{path}: {exc}")
            continue
        problems.extend(validate_receipt(receipt))
    for p in problems:
        print(p)
    print(f"catalog ok: {len(cat['scenarios'])} scenarios "
          f"({len(mandatory_scenarios())} journey-independent); "
          f"{len(problems)} receipt problems in {len(sys.argv) - 1} files")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
