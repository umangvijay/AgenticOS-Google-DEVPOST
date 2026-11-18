"""Specialization evaluation (Phase 15 §15.23) — does the specialist
workflow BEAT the fixed single-agent baseline on the same held-out
tasks, measured by task family? More agents alone is not a success
metric: the comparison demands a measured improvement or an explicit,
quantified tradeoff.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

METRICS = ("verified_completion", "deliverable_quality", "total_cost",
           "end_to_end_latency", "recovery_rate", "unnecessary_delegation",
           "duplicate_work", "human_interruptions", "missing_or_uncertain")


def evaluate_specialization(*, baseline: Dict[str, Any],
                            specialist: Dict[str, Any],
                            by_family: Optional[Dict[str, Dict[str, Any]]] = None,
                            ) -> Dict[str, Any]:
    """Compare the specialist workflow with the baseline over the SAME
    held-out tasks. Missing measurements stay MISSING (never zero); the
    verdict names improvement, tradeoff, or no-case."""
    overall: Dict[str, Any] = {}
    missing = [m for m in METRICS
               if baseline.get(m) is None or specialist.get(m) is None]
    for m in METRICS:
        b, s = baseline.get(m), specialist.get(m)
        if b is None or s is None:
            overall[m] = {"baseline": b, "specialist": s, "delta": None,
                          "verdict": "unmeasured"}
            continue
        delta = round(s - b, 4)
        # Lower-is-better metrics.
        lower_better = m in ("total_cost", "end_to_end_latency",
                             "unnecessary_delegation", "duplicate_work",
                             "human_interruptions", "missing_or_uncertain")
        better = (delta <= 0) if lower_better else (delta >= 0)
        overall[m] = {"baseline": b, "specialist": s, "delta": delta,
                      "verdict": "improved" if better and delta != 0
                      else "regressed" if delta != 0 else "flat"}
    improved = [m for m, v in overall.items() if v["verdict"] == "improved"]
    regressed = [m for m, v in overall.items() if v["verdict"] == "regressed"]
    measured = [m for m in METRICS if m not in missing]
    # The case is decided on the MEASURED metrics; unmeasured ones are
    # reported honestly (missing ≠ zero) but do not veto the verdict.
    if not measured:
        case = "unmeasured"
    elif regressed:
        case = "tradeoff"   # explicit, quantified — the operator decides
    elif improved:
        case = "improvement"
    else:
        case = "no_case"    # more agents alone proves nothing
    return {
        "case": case,
        "overall": overall,
        "by_family": by_family or {},
        "unmeasured": missing,
        "note": ("adding specialists is justified by MEASURED outcomes "
                 "by task family — never by agent count"),
    }


# ── §17.20: document cases in the evaluation dataset ───────────────────

DOCUMENT_CASE_FAMILIES = (
    "correct_document", "fabricated_claims", "numeric_substitution",
    "contradictory_sources", "missing_citations", "corrupted_export",
    "unsafe_preview", "unexecuted_project", "source_correction_after_generation",
)


def document_case_spec(family: str) -> Dict[str, Any]:
    """§17.20: the document-case spec — every family names what a
    passing evaluation must detect."""
    specs = {
        "correct_document": {"expect": "pass",
                             "detect": "nothing to flag"},
        "fabricated_claims": {"expect": "fail/flagged",
                              "detect": "claims without source support"},
        "numeric_substitution": {"expect": "contradiction",
                                 "detect": "changed quantities"},
        "contradictory_sources": {"expect": "flagged",
                                  "detect": "sources disagree"},
        "missing_citations": {"expect": "flagged",
                              "detect": "claims without references"},
        "corrupted_export": {"expect": "fail",
                             "detect": "bytes ≠ manifest digest"},
        "unsafe_preview": {"expect": "contained",
                           "detect": "remote/script vectors"},
        "unexecuted_project": {"expect": "unexecuted badge",
                               "detect": "no runner evidence"},
        "source_correction_after_generation": {"expect": "stale",
                                               "detect": "verification stale"},
    }
    if family not in specs:
        raise ValueError(f"unknown document case family '{family}'")
    return {"family": family, **specs[family],
            "revisions_recorded": True,
            "note": ("generator, template, verifier, source, and artifact "
                     "revisions are recorded with every case (§17.20)")}


def learning_reward_eligible(verification: Dict[str, Any]) -> bool:
    """§17.20: learning rewards consume ONLY authoritative verification
    outcomes — a download, polished appearance, or model claim of success
    is insufficient."""
    if not verification:
        return False
    verdict = verification.get("verdict")
    if verdict == "pass" and verification.get("content_digest"):
        return True   # bound to the exact bytes
    return False


def verifier_correction_ledger(bad_verifier_version: str,
                               good_verifier_version: str,
                               affected_reward_ids: List[str]) -> Dict[str, Any]:
    """§17.20: if a verifier bug invalidates earlier rewards, a
    VERSIONED correction/recomputation is RECORDED — history is never
    silently rewritten."""
    return {"correction": f"{bad_verifier_version} → {good_verifier_version}",
            "affected_rewards": list(affected_reward_ids or []),
            "recompute_required": True,
            "history_rewritten": False,
            "recorded_at": __import__("backend.models.admission",
                                      fromlist=["epoch_now"]).epoch_now()}
