"""MCP protocol validation service (Phase 7 §7.16) — the real acceptance
suite definition and result classification.

The suite defines what a protocol-validated artifact must demonstrate, and
the service classifies OBSERVED results (from a real client run — never
inferred from source structure) into the protocol_validation_status. The
actual exchanges run through MCPClientManager against controlled fixture
providers; this service owns the REQUIRED-SUITE definition so suite changes
invalidate older evidence (§7.19).

Documented protocol profile: MCP 2025-06-18, stdio transport (HTTP transport
is NOT claimed as tested — §7.25: do not advertise untested deployment).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"

# §7.16: the required acceptance matrix. Each case names the observable that
# must be recorded; a skipped required case does NOT count as passed (§6.9).
REQUIRED_PROTOCOL_CASES: List[Dict[str, str]] = [
    {"case": "initialize", "observable": "compatible version + capability negotiation"},
    {"case": "tools_list", "observable": "names and schemas match the artifact"},
    {"case": "tools_call_valid", "observable": "expected result from a controlled fixture"},
    {"case": "tools_call_invalid_input", "observable": "predictable validation failure"},
    {"case": "tools_call_unknown_tool", "observable": "protocol-appropriate error"},
    {"case": "tool_error_preserved", "observable": "tool error status preserved (§7.17)"},
    {"case": "shutdown", "observable": "process and descendants terminate"},
]

CASE_STATUSES = ("passed", "failed", "skipped", "unsupported", "blocked", "uncertain")


def suite_digest() -> str:
    """§7.19: the suite identity — required-suite changes invalidate older
    attestations that were issued against a different suite."""
    return hashlib.sha256(json.dumps(REQUIRED_PROTOCOL_CASES, sort_keys=True).encode()).hexdigest()


def classify_protocol_run(case_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """§7.16: classify OBSERVED case results into the protocol validation
    status. Rules:
    - every required case must be `passed`;
    - a `skipped` required case does NOT count as passed (§6.9);
    - any `failed` → FAILED;
    - `uncertain` → the result is not a protocol-pass claim.
    """
    by_case = {c.get("case"): c.get("status") for c in case_results}
    missing = [c["case"] for c in REQUIRED_PROTOCOL_CASES
               if by_case.get(c["case"]) != "passed"]
    failed = [c.get("case") for c in case_results if c.get("status") == "failed"]
    uncertain = [c.get("case") for c in case_results if c.get("status") == "uncertain"]
    if failed:
        status = "failed"
    elif missing:
        status = "failed"
    elif uncertain:
        status = "not_run"   # an uncertain run is NOT a protocol-pass claim
    else:
        status = "passed"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "status": status,
        "suite_digest": suite_digest(),
        "required_cases": [c["case"] for c in REQUIRED_PROTOCOL_CASES],
        "missing": missing,
        "failed": failed,
        "uncertain": uncertain,
        "case_results": case_results,
    }


def observed_from_client_results(results: Dict[str, Any]) -> List[Dict[str, Any]]:
    """§7.19: derive case results from OBSERVED client behavior (protocol
    messages received, tool results, errors) — never from a workload's
    printed claims. `results` maps case → observed outcome recorded by the
    real client run in mcp_protocol_validator / the fixture harness."""
    out = []
    for case in REQUIRED_PROTOCOL_CASES:
        observed = results.get(case["case"], "skipped")
        status = observed if observed in CASE_STATUSES else "skipped"
        out.append({"case": case["case"], "status": status,
                    "observable": case["observable"]})
    return out
