"""Outcome Verifier (audit §9, first subagent): a bounded, read-only
checker that validates task outputs against declared acceptance criteria.

Permission boundary: READ-ONLY by default. It never executes tools, never
grants permissions, and never repairs — it only PASS/FAIL/NEEDS-REVIEW
with reasons, so a model cannot approve its own work by declaring "done".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AcceptanceCriterion:
    kind: str                     # non_empty | contains | regex | http_ok | artifact_exists | schema_keys | browser_postcondition
    value: Any = None
    description: str = ""

    def check(self, output: Dict[str, Any]) -> tuple[bool, str]:
        if self.kind == "non_empty":
            target = output.get(self.value) if isinstance(self.value, str) else output
            ok = bool(target) and str(target).strip() != ""
            return ok, "" if ok else f"expected non-empty '{self.value or 'output'}'"
        if self.kind == "contains":
            haystack = str(output.get("reply") or output.get("message") or "")
            ok = str(self.value).casefold() in haystack.casefold()
            return ok, "" if ok else f"output does not mention '{self.value}'"
        if self.kind == "http_ok":
            code = output.get("status_code")
            ok = isinstance(code, int) and 200 <= code < 300
            return ok, "" if ok else f"expected 2xx, got {code}"
        if self.kind == "artifact_exists":
            # §10.8 fix: a reference must be RESOLVED and checked, not merely
            # present in a dictionary. The verifier accepts a resolver callable
            # (injected by the engine) — without one, the criterion is
            # needs-review (unverifiable), never a silent pass.
            ref = output.get("artifact_id") or output.get("file_path")
            if not ref:
                return False, "no artifact was produced"
            resolver = (self.value or {}).get("resolver") if isinstance(self.value, dict) else None
            if resolver is None:
                return False, ("UNVERIFIABLE: artifact reference present but "
                               "unresolved — no artifact resolver was supplied (§10.8)")
            # §17.9 (finding E): NEVER convert a result dictionary to a
            # success boolean — a {"verified": false, ...} result is a
            # FAIL, and a structured dict is inspected for its verdict.
            try:
                result = resolver(ref)
            except Exception as e:
                return False, f"artifact resolution failed: {e}"
            # §17.9 (new probe 1): TYPED results may be dict-like OR
            # objects (dataclasses/models) — read the verdict from
            # either; bool(obj) on a non-dict is NEVER evidence.
            # §18.3: coroutines and unknown objects are NEVER evidence —
            # only dicts or typed results carrying verdict/verified.
            if asyncio.iscoroutine(result) or \
                    asyncio.iscoroutinefunction(getattr(result, "send", None)):
                return False, ("UNVERIFIABLE: the resolver returned a "
                               "coroutine — call it and await the result")
            def _get(key, default=None):
                if isinstance(result, dict):
                    return result.get(key, default)
                return getattr(result, key, default)
            if isinstance(result, dict) or hasattr(result, "verdict") \
                    or hasattr(result, "verified"):
                if _get("verified") is False:
                    reason = _get("reason") or "verification failed"
                    return False, f"artifact verification failed: {reason}"
                if _get("verdict") in ("fail", "unavailable",
                                       "needs_review"):
                    return (False, f"artifact verdict: "
                            f"{_get('verdict')}")
                if _get("verdict") == "pass" or _get("verified") is True:
                    return True, ""
                # A dict WITHOUT a verdict is not proof of existence.
                return False, ("UNVERIFIABLE: the resolver returned "
                               "structured data without a verdict")
            # Truthiness NEVER promotes an unrecognized object.
            if isinstance(result, (str, int, float, bool, type(None))):
                exists = bool(result)
                return exists, "" if exists else f"artifact {ref} does not exist"
            return False, ("UNVERIFIABLE: the resolver returned an "
                           "unrecognized object type")
        if self.kind == "schema_keys":
            keys = output.keys()
            missing = [k for k in (self.value or []) if k not in keys]
            return not missing, "" if not missing else f"missing keys: {', '.join(missing)}"
        if self.kind == "browser_postcondition":
            # §14.I: the browser postcondition validates against the
            # RUNTIME-CAPTURED authoritative state (`browser_evidence` —
            # the observation the page actually presented at the done
            # decision), NEVER against the model's claimed narrative
            # alone. Without the runtime evidence the criterion is
            # UNVERIFIABLE (needs review) — a model cannot attest its
            # own browser postcondition.
            evidence = output.get("browser_evidence")
            if not isinstance(evidence, dict) or not evidence:
                return False, ("UNVERIFIABLE: no authoritative browser "
                               "evidence was captured for this task "
                               "(the runtime observation is required)")
            expected = self.value or {}
            if isinstance(expected, str):
                expected = {"marker": expected}
            problems = []
            marker = str(expected.get("marker") or "")
            if marker:
                # The marker must appear in the AUTHORITY fields — the
                # observed URL/title — not the model's result text.
                authority = " ".join([
                    str(evidence.get("final_url") or ""),
                    str(evidence.get("page_title") or ""),
                ]).casefold()
                if marker.casefold() not in authority:
                    problems.append(
                        f"'{marker}' is not present in the observed "
                        "URL/title")
            url_contains = expected.get("url_contains")
            if url_contains and str(url_contains).casefold() not in                     str(evidence.get("final_url") or "").casefold():
                problems.append(
                    f"observed URL does not contain '{url_contains}'")
            min_steps = expected.get("min_executed_steps")
            if min_steps is not None and                     int(evidence.get("executed_step_count") or 0) < int(min_steps):
                problems.append(
                    f"only {evidence.get('executed_step_count')} executed "
                    f"steps (expected >= {min_steps})")
            if problems:
                return False, "; ".join(problems)
            return True, ""
        if self.kind in ("semantic_match", "readability"):
            # §12.19: judge-eligible kinds await a model judge — the
            # deterministic verifier marks them UNVERIFIABLE (needs_review),
            # never a hard fail and never a silent pass.
            return False, ("UNVERIFIABLE: awaiting model judge for "
                           f"'{self.kind}'")
        return False, f"unknown criterion kind: {self.kind}"


@dataclass
class VerificationResult:
    verdict: str                  # pass | fail | needs_review
    reasons: List[str] = field(default_factory=list)
    checked: int = 0

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


class OutcomeVerifier:
    """Read-only verification of task outputs. The engine consults this
    before marking a deliverable-producing task complete (audit §5 stage 1
    outcome measurement)."""

    def verify(self, output: Optional[Dict[str, Any]],
               criteria: Optional[List[AcceptanceCriterion]] = None) -> VerificationResult:
        output = output or {}
        reasons: List[str] = []
        checked = 0

        # §10.8: a bare non-empty reply is NOT a pass — with NO acceptance
        # criteria the verdict is needs_review (the operator or a profile
        # must supply criteria; "output is non-empty" is not completion
        # evidence, §10.8).
        has_content = any(
            isinstance(v, (str, dict, list)) and v
            for v in output.values()
        )
        if not has_content:
            return VerificationResult("fail", ["output is empty"], 0)

        for criterion in (criteria or []):
            checked += 1
            ok, reason = criterion.check(output)
            if not ok:
                reasons.append(reason or f"criterion '{criterion.kind}' failed")

        if not criteria:
            # §10.8 fix: "no acceptance criteria" cannot silently verify —
            # needs_review, not pass.
            return VerificationResult("needs_review",
                                      ["no acceptance criteria were supplied — "
                                       "output completeness cannot be verified"], 0)
        unverifiable = [r for r in reasons if r.startswith("UNVERIFIABLE:")]
        if unverifiable:
            # §10.8: an unverifiable criterion is needs-review, not a silent
            # pass and not a hard fail (the operator decides).
            clean = [r for r in reasons if not r.startswith("UNVERIFIABLE:")]
            return VerificationResult("needs_review",
                                      [r.replace("UNVERIFIABLE: ", "")
                                       for r in reasons], checked)
        if reasons:
            # Needs-review (not hard fail) when SOME criteria passed: the
            # operator decides, per §5 — verification never silently passes.
            verdict = "needs_review" if checked > len(reasons) else "fail"
            return VerificationResult(verdict, reasons, checked)
        return VerificationResult("pass", [], checked)
