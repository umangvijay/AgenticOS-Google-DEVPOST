"""Specialist verification agents (Phase 15 §15.1/§15.2).

Two LLM specialists that run through the SAME bounded task contracts as
every other delegation — they PROPOSE verdicts/evidence; the
deterministic gates (acceptance criteria, effect ledger, artifact
checks) remain the authority:

- OutcomeVerifierAgent (§15.1): checks whether a task's result meets
  the request against its acceptance criteria. Receives EVIDENCE ONLY
  (the output, the criteria, the effect/artifact references) — never
  tools, never the authority to repeat the underlying action.
- ConnectorValidatorAgent (§15.2): proposes operation-specific TEST
  CASES for a connector's required operations (auth denial, required
  parameters, pagination, malformed responses, controlled writes).
  Cannot invent passing results — the generated cases execute through
  the §6 validation pipeline which records the actual outcomes.

Both agents output STRICT JSON schemas (pydantic), cannot execute
tools, and their proposals are RECORDED with provenance (§12.19:
judges supplement deterministic evidence; a judge's confident
explanation cannot validate a nonexistent artifact).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# noqa ordering fix below

from pydantic import BaseModel, Field

import logging
logger = logging.getLogger(__name__)


# ── Output schemas (strict, tool-less) ─────────────────────────────────

class VerificationVerdict(BaseModel):
    """The Outcome Verifier's structured verdict over ONE task result."""
    verdict: str = Field(
        description="pass | fail | needs_review — pass ONLY when every "
                    "criterion is demonstrably met by the evidence.")
    criterion_results: List[Dict[str, Any]] = Field(
        description="Per-criterion: {criterion, met, reasoning<=200 chars}.")
    missing_evidence: List[str] = Field(
        default_factory=list,
        description="Evidence the verdict NEEDS but was not supplied — "
                    "e.g. the artifact reference was unresolved.")
    reasoning: str = Field(description="<= 300 chars, evidence-grounded.")


class ValidationTestPlan(BaseModel):
    """The Connector Validator's proposed test cases for ONE operation.
    Proposals only — execution goes through the §6 validation pipeline."""
    operation_id: str
    test_cases: List[Dict[str, Any]] = Field(
        description="Cases: {name, kind: auth_denial|required_params|"
                    "pagination|malformed_response|timeout|controlled_write, "
                    "request_hint, expected_observation}.")
    risks: List[str] = Field(
        default_factory=list,
        description="What could go wrong running these cases (rate limits, "
                    "side effects).")


# ── Context models (typed inputs per §15.4) ────────────────────────────

class VerificationContext(BaseModel):
    task_id: str
    run_id: str
    goal: str
    criteria: List[Dict[str, Any]] = Field(default_factory=list)
    output_data: Dict[str, Any] = Field(default_factory=dict)
    # Evidence references (§15.1: evidence, not authority):
    effect_states: Dict[str, str] = Field(default_factory=dict)
    artifact_reference: Optional[str] = None
    deterministic_verdict: str = "needs_review"
    deterministic_reasons: List[str] = Field(default_factory=list)


class ValidationContext(BaseModel):
    connector_id: str
    revision_id: Optional[str] = None
    operation_id: str
    operation_schema: Dict[str, Any] = Field(default_factory=dict)
    auth_method: str = "api_key"
    existing_coverage: List[str] = Field(default_factory=list)


# ── Agent constructors (ADK, structured output, no tools) ──────────────

def get_verifier_agent(model: Optional[str] = None):
    """The Outcome Verifier: evidence-only, no tools, structured verdict.
    Its proposal RECORDED alongside the deterministic verdict; the
    combination rule (§12.19) applies — a judge's pass never upgrades a
    deterministic fail."""
    from google.adk.agents.llm_agent import LlmAgent
    from backend.services.llm_context import adk_gemini

    instruction = """
    You are AgentOS's Outcome Verifier. You check whether a task's result
    meets the request against its acceptance criteria.

    You receive EVIDENCE ONLY: the output, the criteria, the deterministic
    verifier's verdict with reasons, and effect/artifact references.

    RULES:
    1. You have NO tools and NO authority to repeat the underlying action.
    2. Judge ONLY from the supplied evidence. If evidence is missing, list
       it in missing_evidence and use needs_review.
    3. A criterion is met only when the evidence DEMONSTRABLY shows it —
       confident prose never substitutes for evidence.
    4. Return strictly valid JSON matching VerificationVerdict.
    """
    return LlmAgent(name="OutcomeVerifierAgent",
                    instruction=instruction,
                    model=adk_gemini(model),
                    output_schema=VerificationVerdict)


def get_validator_agent(model: Optional[str] = None):
    """The Connector Validator: proposes operation-specific test cases.
    Cannot invent passing results — the §6 pipeline executes and records."""
    from google.adk.agents.llm_agent import LlmAgent
    from backend.services.llm_context import adk_gemini

    instruction = """
    You are AgentOS's Connector Validator. You propose operation-specific
    test cases that establish trustworthy evidence for ONE connector
    operation.

    RULES:
    1. You propose CASES only — you cannot execute them and cannot invent
       passing results. The validation pipeline executes and records the
       actual outcomes.
    2. Cover: authentication denial, required-parameter enforcement,
       pagination (if the schema suggests lists), malformed-response
       handling, and controlled writes ONLY when the operation writes.
    3. Flag risks (rate limits, side effects) in risks.
    4. Return strictly valid JSON matching ValidationTestPlan.
    """
    return LlmAgent(name="ConnectorValidatorAgent",
                    instruction=instruction,
                    model=adk_gemini(model),
                    output_schema=ValidationTestPlan)


# ── Contract-bound execution (typed inputs/outputs, §15.4) ─────────────

def _parse_agent_output(raw: Any) -> Dict[str, Any]:
    """Normalize an injected judge's return: dict | ADK-style events
    (objects with .output) | pydantic models | a bare value."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and raw:
        first = raw[0]
        if hasattr(first, "output"):
            out = first.output
            if isinstance(out, dict):
                return out
            if hasattr(out, "model_dump"):
                return out.model_dump()
            return {}
        if hasattr(first, "model_dump"):
            return first.model_dump()
        return first if isinstance(first, dict) else {}
    if raw is not None and hasattr(raw, "output"):
        out = raw.output
        if isinstance(out, dict):
            return out
        if hasattr(out, "model_dump"):
            return out.model_dump()
    if raw is not None and hasattr(raw, "model_dump"):
        return raw.model_dump()
    return {}


def _judge_accepts_dict(judge_client) -> bool:
    """Inspect the injected judge's signature: context-dict or no-arg."""
    import inspect
    try:
        sig = inspect.signature(judge_client)
        return len(sig.parameters) >= 1
    except (TypeError, ValueError):
        return False


VERIFIER_OUTPUT_SCHEMA = {"required": ["verdict", "criterion_results",
                                       "reasoning"]}
VALIDATOR_OUTPUT_SCHEMA = {"required": ["operation_id", "test_cases"]}


async def run_verifier_via_contract(contract_repo, verification: VerificationContext,
                                    *, model: Optional[str] = None,
                                    judge_client: Optional[Any] = None,
                                    parent_id: str = "engine",
                                    principal_id: Optional[str] = None,
                                    org_id: Optional[str] = None,
                                    depth: int = 0,
                                    authorization_context: Optional[Dict[str, Any]] = None,
                                    ) -> Dict[str, Any]:
    """Delegate verification through a bounded contract: create → claim →
    run the verifier agent (or the deterministic fallback when no model
    client is configured) → validate the typed output → complete. The
    COMBINED verdict follows §12.19: the deterministic verdict's fail
    stands; a judge pass never upgrades it."""
    from backend.services.agent_authority import (
        require_specialist_access, resolve_principal_from_parent)
    from backend.services.agent_contracts import claim_specialist, delegate, complete, fail
    from backend.services.model_judge import combine_verdicts, JudgeResult
    # §27.2: a run ID identifies WORK, never an actor. The principal is
    # the authenticated caller or the parent run's PERSISTED owner;
    # unresolved authority fails closed (no silent run-ID principal).
    principal = principal_id
    if not principal:
        principal = await resolve_principal_from_parent("run", parent_id)
    if not principal:
        from backend.services.agent_contracts import AgentTaskError
        raise AgentTaskError(
            f"protected verification cannot resolve an actor: no "
            f"authenticated principal and parent run {parent_id} has no "
            "persisted owner", "AUTHORITY_UNRESOLVED")
    await require_specialist_access(principal, authorization_context, org_id)
    contract = await delegate(
        parent_kind="run", parent_id=parent_id,
        role=__import__("backend.models.agent_task",
                        fromlist=["AgentRole"]).AgentRole.OUTCOME_VERIFIER,
        tenant_id=principal, input_data=verification.model_dump(),
        output_schema=VERIFIER_OUTPUT_SCHEMA,
        allowed_tools=["evidence.read", "criteria.verify"],
        # §15.14: the criteria are FROZEN on the contract at delegation —
        # the producer cannot change them after seeing the output. A
        # context without explicit criteria still gets the registry's
        # "a structured verdict exists" bar (never ambiguous).
        acceptance_criteria=(list(verification.criteria or []) or
                             [dict(c) for c in __import__(
                                 "backend.services.agent_task_contracts",
                                 fromlist=["SPECIALIST_REGISTRY"]
                             ).SPECIALIST_REGISTRY[__import__(
                                 "backend.models.agent_task",
                                 fromlist=["AgentRole"]
                             ).AgentRole.OUTCOME_VERIFIER].get(
                                 "default_acceptance", [])]),
        dedup_key=f"verify:{verification.run_id}:{verification.task_id}",
        depth=depth, repo=contract_repo)
    actor = await claim_specialist(contract_repo, contract.contract_id, "verifier")
    try:
        if judge_client is None:
            # No model configured: the deterministic verdict stands, the
            # contract completes honestly with it (never fabricated).
            result = {"verdict": verification.deterministic_verdict,
                      "criterion_results": [
                          {"criterion": r, "met": False, "reasoning": reason}
                          for r, reason in zip(
                              verification.criteria,
                              verification.deterministic_reasons)] or
                          [{"criterion": "deterministic", "met": verification.deterministic_verdict == "pass",
                            "reasoning": "; ".join(
                                verification.deterministic_reasons) or "no criteria"}],
                      "missing_evidence": [],
                      "reasoning": "deterministic verdict (no judge model configured)"}
        elif judge_client is not None:
            # The injected judge (tests / alternate judge backends) — the
            # ADK/budget path is NOT required for verification proposals.
            raw = await judge_client(
                verification.model_dump_json()[:8000]) \
                if _judge_accepts_dict(judge_client) else \
                await judge_client()
            parsed = _parse_agent_output(raw)
            result = parsed or {
                "verdict": "needs_review",
                "criterion_results": [],
                "missing_evidence": ["judge returned no usable output"],
                "reasoning": "judge unavailable"}
        # §12.19 combination: deterministic fail stands.
        combined = combine_verdicts(
            verification.deterministic_verdict,
            JudgeResult(verdict=result.get("verdict", "needs_review"),
                        reasoning=result.get("reasoning", ""),
                        model=model or "none"))
        result["combined_verdict"] = combined
        result["deterministic_verdict"] = verification.deterministic_verdict
        await require_specialist_access(principal, authorization_context, org_id)
        done = await complete(contract.contract_id, result,
                              actor=actor,
                              repo=contract_repo)
        # §15.12 (audit W6): the handoff to downstream specialists is
        # TYPED — verified observations carry the evidence; failures and
        # unresolved criteria are explicit limitations/questions, never
        # buried in prose. All items are content-classified.
        from backend.services.handoffs import (
            ContentClass, Handoff, HandoffItem,
        )
        _limitations = [c.get("reason") for c in
                        (result.get("criterion_results") or [])
                        if isinstance(c, dict) and c.get("passed") is False]
        _unresolved = [c.get("criterion") for c in
                       (result.get("criterion_results") or [])
                       if isinstance(c, dict) and c.get("passed") is None]
        handoff = Handoff(
            from_role="outcome_verifier", to_role="orchestrator",
            results={"verdict": combined,
                     "deterministic_verdict": result.get(
                         "deterministic_verdict")},
            evidence_refs=[done.contract_id],
            limitations=[str(x) for x in _limitations if x],
            unresolved_questions=[str(x) for x in _unresolved if x],
            items=[HandoffItem(ContentClass.VERIFIED_OBSERVATION,
                               {"combined_verdict": combined},
                               authority=False),
                   HandoffItem(ContentClass.AGENT_PROPOSAL,
                               result.get("reasoning", ""),
                               authority=False)])
        return {"contract_id": done.contract_id, "result": result,
                "combined_verdict": combined,
                "handoff": {"from_role": handoff.from_role,
                            "to_role": handoff.to_role,
                            "results": handoff.results,
                            "evidence_refs": handoff.evidence_refs,
                            "limitations": handoff.limitations,
                            "unresolved_questions":
                                handoff.unresolved_questions,
                            "authority_items": 0}}
    except Exception as exc:
        await fail(contract.contract_id, str(exc),
                   actor=actor, repo=contract_repo)
        raise
