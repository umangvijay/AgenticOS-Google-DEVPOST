"""Grounded deliverables (Phase 15 §15.21) — the deliverable specialist
assembles VERIFIED inputs and retains provenance. Supplied user facts
are preserved EXACTLY; anything not traceable to a supplied fact or a
verified observation is FLAGGED as an unsupported addition — never
silently included.
"""
from __future__ import annotations

from typing import Any, Dict, List


def _values_conflict(supplied_value: Any, draft_value: Any) -> bool:
    """§17.11 (finding E): key PRESENCE is not support — the VALUES must
    agree. A different employer under the same key is a contradiction,
    not a preserved fact."""
    s = str(supplied_value or "").strip().lower()
    d = str(draft_value or "").strip().lower()
    if not s or not d:
        return False
    return s != d and d not in s


def check_resume_facts(*, supplied: Dict[str, Any],
                       draft: Dict[str, Any]) -> Dict[str, Any]:
    """For resumes: compare the DRAFT against the SUPPLIED facts.
    Employers, dates, qualifications, and achievements present in the
    draft but not in the supplied facts (and not in verified
    observations) are flagged — the deliverable cannot invent user
    history. A CHANGED value under an existing key is flagged as a
    CONTRADICTION (finding E: key presence never proved support)."""
    supported_keys = {k for k, v in supplied.items() if v}
    flagged: List[Dict[str, Any]] = []
    preserved: List[str] = []
    for key, value in draft.items():
        if not value:
            continue
        if key in supported_keys:
            if _values_conflict(supplied.get(key), value):
                flagged.append({
                    "field": key,
                    "value": value,
                    "supplied": supplied.get(key),
                    "reason": "CONTRADICTION: the draft value differs from "
                              "the supplied fact under the same field",
                    "next_action": "reconcile with the user-supplied value",
                })
            else:
                preserved.append(key)
        else:
            flagged.append({
                "field": key,
                "value": value,
                "reason": "not present in the user-supplied facts and "
                          "not backed by a verified observation",
                "next_action": "remove it or supply the supporting fact",
            })
    return {
        "preserved": sorted(preserved),
        "unsupported_additions": flagged,
        "grounded": not flagged,
        "summary": (f"{len(preserved)} supplied fields preserved; "
                    f"{len(flagged)} unsupported addition(s) flagged"),
    }


def check_report_sources(*, claims: List[Dict[str, Any]],
                         verified_fact_ids: List[str]) -> Dict[str, Any]:
    """For reports: every claim is classified — source fact, derived
    calculation, or UNCERTAIN conclusion. Unbacked claims never masquerade
    as facts."""
    verified = set(verified_fact_ids or [])
    classification = {"source_fact": [], "calculation": [],
                      "uncertain_conclusion": [], "unbacked": []}
    for c in claims or []:
        kind = c.get("kind")
        if kind == "source_fact":
            # §17.11/§17.12 (finding E): a SELF-DECLARED source_fact is
            # NOT proof — its fact_id must resolve to a VERIFIED source,
            # else it is unbacked (a label cannot vouch for itself).
            if c.get("fact_id") in verified:
                classification["source_fact"].append(c.get("claim_id"))
            else:
                classification["unbacked"].append(c.get("claim_id"))
        elif kind in classification:
            classification[kind].append(c.get("claim_id"))
        elif c.get("fact_id") in verified:
            classification["source_fact"].append(c.get("claim_id"))
        else:
            classification["unbacked"].append(c.get("claim_id"))
    return {"classification": classification,
            "grounded": not classification["unbacked"]}
