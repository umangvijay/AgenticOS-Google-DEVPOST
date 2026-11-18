"""Versioned evaluation datasets (Phase 13 §13.11).

Separate DEVELOPMENT, HELD-OUT, and ADVERSARIAL sets with provenance and
expected outcomes:

- Case families required by §13.11: supported successes, unsupported
  tasks, missing credentials, ambiguous outcomes, malformed outputs,
  factuality failures.
- The HELD-OUT set is draft-proof: only principals with the APPROVE
  capability (operators) may modify it — the drafting agent can propose
  development cases but can NEVER touch held-out expectations or select
  only favorable cases (the held-out set is used verbatim at evaluation).
- Experiments pin their dataset by (dataset_id, version, hash) in the
  §13.10 manifest — candidate tuning cannot silently modify the
  held-out cases that judge it.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now
from backend.services.principals import Principal, principal_from_actor, CAP_APPROVE

SET_KINDS = ("development", "heldout", "adversarial")

# §13.11: the required case families — a compliant dataset includes at
# least one case of each.
REQUIRED_CASE_FAMILIES = (
    "supported_success", "unsupported_task", "missing_credentials",
    "ambiguous_outcome", "malformed_output", "factuality_failure",
)


def new_dataset_id() -> str:
    return f"eds_{uuid.uuid4().hex[:20]}"


def dataset_version_hash(cases: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(cases, sort_keys=True, default=str).encode()).hexdigest()[:32]


class DatasetError(Exception):
    pass


class EvaluationDatasetService:
    """SQLite reference implementation."""

    def __init__(self, db):
        self.db = db

    async def create_dataset(self, *, name: str, set_kind: str,
                             cases: List[Dict[str, Any]],
                             created_by: str,
                             provenance: str = "") -> Dict[str, Any]:
        if set_kind not in SET_KINDS:
            raise DatasetError(
                f"set_kind must be one of {SET_KINDS}, got {set_kind}")
        _validate_case_families(cases, set_kind)
        now = epoch_now()
        dataset_id = new_dataset_id()
        await self.db.execute(
            """INSERT INTO evaluation_datasets
               (dataset_id, name, set_kind, version, cases_json,
                provenance, created_by, created_at)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (dataset_id, name, set_kind, json.dumps(cases or []),
             provenance, created_by, now))
        await self.db.commit()
        return await self.get_dataset(dataset_id)

    async def update_cases(self, dataset_id: str, *, cases: List[Dict[str, Any]],
                           actor: str) -> Dict[str, Any]:
        """§13.11: held-out expectations are DRAFT-PROOF — only a
        principal with the APPROVE capability (an operator) may modify
        them. Editing creates a NEW version (history preserved)."""
        principal: Optional[Principal] = principal_from_actor(actor)
        current = await self.get_dataset(dataset_id)
        if current is None:
            raise DatasetError("Dataset not found")
        if current["set_kind"] == "heldout" and \
                (principal is None or not principal.can(CAP_APPROVE)):
            raise DatasetError(
                "The held-out set is draft-proof: modifying its "
                "expectations requires an operator with approval "
                "authority (§13.11)")
        _validate_case_families(cases, current["set_kind"])
        now = epoch_now()
        await self.db.execute(
            """INSERT INTO evaluation_datasets
               (dataset_id, name, set_kind, version, cases_json,
                provenance, created_by, created_at)
               SELECT dataset_id, name, set_kind, version + 1, ?, provenance,
                      created_by, ?
               FROM evaluation_datasets WHERE dataset_id=? AND version=?""",
            (json.dumps(cases or []), now, dataset_id, current["version"]))
        await self.db.commit()
        return await self.get_dataset(dataset_id, version=current["version"] + 1)

    async def get_dataset(self, dataset_id: str,
                          version: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if version is None:
            cursor = await self.db.execute(
                "SELECT * FROM evaluation_datasets WHERE dataset_id=? "
                "ORDER BY version DESC LIMIT 1", (dataset_id,))
        else:
            cursor = await self.db.execute(
                "SELECT * FROM evaluation_datasets WHERE dataset_id=? AND "
                "version=?", (dataset_id, version))
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["cases"] = json.loads(d.pop("cases_json") or "[]")
        d["version_hash"] = dataset_version_hash(d["cases"])
        return d

    @staticmethod
    def heldout_cases(dataset: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The cases used verbatim at evaluation — NEVER filtered by the
        drafting agent."""
        return list(dataset.get("cases") or [])


def _validate_case_families(cases: List[Dict[str, Any]], set_kind: str) -> None:
    """§13.11: the required case families per set kind — a compliant
    dataset covers supported successes, unsupported tasks, missing
    credentials, ambiguous outcomes, malformed outputs, and factuality
    failures."""
    if set_kind != "heldout":
        return   # dev/adversarial sets are free-form
    families = {c.get("family") for c in cases or []}
    missing = [f for f in REQUIRED_CASE_FAMILIES if f not in families]
    if missing:
        raise DatasetError(
            f"A held-out set must cover the required case families; "
            f"missing: {', '.join(missing)}")


class SplitAuditError(DatasetError):
    """The train/held-out split is contaminated — an improvement claim
    judged on it would be measuring training-fixture exposure."""


def _fixture_identity(case: Dict[str, Any]) -> str:
    """A canonical identity for near-duplicate detection: family plus a
    normalized digest of the case's stimuls (goal/prompt/inputs)."""
    stimulus = {
        k: case.get(k)
        for k in ("goal", "prompt", "input", "inputs", "task_input",
                  "expected_behavior")
        if case.get(k) is not None
    }
    material = json.dumps(
        {"family": case.get("family"), "stimulus": stimulus},
        sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def audit_dataset_split(*, development: List[Dict[str, Any]],
                        heldout: List[Dict[str, Any]],
                        adversarial: List[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """§29.24: a REPRODUCIBLE audit of the train/eval split.

    Hard failures (SplitAuditError):
    - the same case_id in more than one set — the held-out judgment is
      contaminated by definition;
    - a near-duplicate fixture (same family + stimulus digest) shared
      between development and held-out — tuning on it is tuning on the
      judge.

    The report carries the held-out denominator, per-family counts per
    set, and same-set near-duplicate warnings (a family duplicated
    inside one set is a data-quality smell, not contamination).
    """
    sets = {"development": list(development or []),
            "heldout": list(heldout or []),
            "adversarial": list(adversarial or [])}

    # 1) Case ids must be unique ACROSS sets.
    id_owner: Dict[str, str] = {}
    for set_name, cases in sets.items():
        for case in cases:
            cid = str(case.get("case_id") or case.get("id") or "")
            if not cid:
                continue
            if cid in id_owner and id_owner[cid] != set_name:
                raise SplitAuditError(
                    f"case '{cid}' appears in both {id_owner[cid]} and "
                    f"{set_name} — the split is contaminated")
            id_owner[cid] = set_name

    # 2) Near-duplicate fixtures across dev↔heldout are contamination.
    identity_owner: Dict[str, str] = {}
    for set_name in ("development", "heldout", "adversarial"):
        for case in sets[set_name]:
            identity = _fixture_identity(case)
            prior = identity_owner.get(identity)
            if prior and prior != set_name:
                if {prior, set_name} & {"development", "heldout"}:
                    raise SplitAuditError(
                        f"a {prior} fixture is near-duplicated in "
                        f"{set_name} (same family and stimulus) — "
                        "held-out exposure is not held-out")
            identity_owner.setdefault(identity, set_name)

    # 3) Same-set duplicates are warnings (quality, not contamination).
    warnings = []
    for set_name, cases in sets.items():
        seen: Dict[str, int] = {}
        for case in cases:
            identity = _fixture_identity(case)
            seen[identity] = seen.get(identity, 0) + 1
        duplicated = {k: v for k, v in seen.items() if v > 1}
        if duplicated:
            warnings.append(
                f"{set_name}: {sum(duplicated.values())} cases share "
                f"{len(duplicated)} identical family+stimulus digests")

    families_by_set = {
        set_name: sorted({str(c.get("family")) for c in cases if c.get("family")})
        for set_name, cases in sets.items()
    }
    return {
        "audit": "dataset_split",
        "heldout_denominator": len(sets["heldout"]),
        "counts": {k: len(v) for k, v in sets.items()},
        "families_by_set": families_by_set,
        "heldout_family_coverage": {
            family: (family in families_by_set["heldout"])
            for family in REQUIRED_CASE_FAMILIES},
        "warnings": warnings,
        "contaminated": False,
    }


_repo = None


def set_dataset_repo(repo) -> None:
    global _repo
    _repo = repo


def get_dataset_repo():
    return _repo
