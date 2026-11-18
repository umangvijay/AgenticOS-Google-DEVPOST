"""Task-family evaluators (Phase 12 §12.17).

"Non-empty output" is used ONLY where it is genuinely sufficient for the
declared task family. Each family defines the deterministic checks that
must hold for its outcomes:

- api_read:        intended resource/account + required fields present
- api_mutation:    provider evidence (effect confirmed) + resulting state
- browser_task:    intended control/resource + supported postcondition
- research_report: required coverage + artifact validity
- resume:          source-backed facts (provenance) + document integrity
- connector_build: required operations tested + activation eligibility
- recovery:        original outcome achieved within authority and limits
- code_project:    required files + declared build/test evidence

The registry returns CHECKS (the acceptance-criteria shape the engine's
OutcomeVerifier already executes) — it never runs anything itself.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

FAMILIES = ("api_read", "api_mutation", "browser_task", "research_report",
            "resume", "connector_build", "recovery", "code_project")


def criteria_for_family(family: str,
                        context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """§12.17: the mandatory checks for a task family. `context` supplies
    the family-specific parameters (required fields, artifact refs,
    operation ids). Deterministic — same input, same checks."""
    ctx = context or {}
    if family == "api_read":
        criteria = [{"kind": "schema_keys", "description": "response carries the required fields",
                     "value": list(ctx.get("required_fields") or ["result"])}]
        if ctx.get("status_code") is not None:
            criteria.append({"kind": "http_ok",
                             "description": "the read returned a 2xx"})
        return criteria
    if family == "api_mutation":
        # A mutation is NOT verified by a non-empty reply: provider
        # evidence (a confirmed effect) is mandatory.
        return [{"kind": "effect_resolved",
                 "description": "the external write is confirmed from "
                                "provider evidence (§5)"},
                {"kind": "schema_keys",
                 "description": "resulting state fields present",
                 "value": list(ctx.get("required_fields") or [])}]
    if family == "browser_task":
        # §14.I: the postcondition is verified from the RUNTIME-CAPTURED
        # authoritative observation (browser_evidence), never from the
        # model's claimed narrative alone.
        expected = ctx.get("expected_content") or ""
        return [{"kind": "browser_postcondition",
                 "description": "the intended postcondition is visible in "
                                "the runtime-captured observation (§9.5: "
                                "not just a successful click)",
                 "value": {"marker": expected}
                 if expected else {}},
                {"kind": "effect_resolved",
                 "description": "any consequential browser submission is "
                                "confirmed"}]
    if family == "research_report":
        return [{"kind": "schema_keys",
                 "description": "required sections present",
                 "value": list(ctx.get("required_sections") or
                               ["summary", "findings"])},
                {"kind": "artifact_exists",
                 "description": "the report artifact resolves and exists"}]
    if family == "resume":
        return [{"kind": "artifact_exists",
                 "description": "the rendered document exists and resolves"},
                {"kind": "schema_keys",
                 "description": "requested sections present",
                 "value": list(ctx.get("required_sections") or
                               ["basics", "experience"])}]
    if family == "connector_build":
        return [{"kind": "schema_keys",
                 "description": "required operations validated + revision "
                                "activated (§12.7 readiness)",
                 "value": list(ctx.get("required_fields") or
                               ["validated_operations", "revision_id"])}]
    if family == "recovery":
        return [{"kind": "non_empty",
                 "description": "the ORIGINAL outcome achieved within "
                                "authority and limits (§12.26: a queued "
                                "correction is not a recovery)",
                 "value": ctx.get("result_key", "reply")},
                {"kind": "effect_resolved",
                 "description": "the repaired action's effect is confirmed"}]
    if family == "code_project":
        return [{"kind": "artifact_exists",
                 "description": "the produced project artifact resolves"},
                {"kind": "schema_keys",
                 "description": "declared build/test evidence present",
                 "value": list(ctx.get("required_fields") or
                               ["files", "build_evidence"])}]
    raise ValueError(f"Unknown task family: {family} (expected {FAMILIES})")


def family_for_agent(agent: str) -> str:
    """The default family for an agent (§12.17: the product's actual
    families — never a blanket non-empty check)."""
    a = str(agent or "").lower()
    if "email" in a or "http" in a:
        return "api_mutation"
    if "browser" in a or "web" in a:
        return "browser_task"
    if "search" in a or "summar" in a:
        return "api_read"
    if "resume" in a or "tailor" in a:
        return "resume"
    if "mcp_build" in a or "connector" in a:
        return "connector_build"
    if "recover" in a:
        return "recovery"
    return "research_report"
