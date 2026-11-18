"""Memory governance (Phase 16 §16.1–16.6, §16.14, §16.19, §16.22, §16.32).

The governed layer OVER the existing memory lifecycle — extraction
proposes, governance decides:

- §16.1 EXPLICIT scopes: task < workflow < personal < project <
  organization < published. Sharing is an AUTHORIZED OPERATION, never
  an inference from org membership — creating an org or adding a member
  cannot expose historical personal memories.
- §16.3 write policy by class: what agents may remember automatically
  (verified outcomes, explicitly instructed preferences) vs what needs
  confirmation (consequential personal facts, uncertain inferences,
  proposed procedures). The record says WHY it was stored and whether
  it is explicit or inferred.
- §16.4 secrets stay OUT of ordinary memory: sensitivity checks run
  BEFORE persistence, indexing, summarization, and export — only
  REFERENCES to credential-store entries may be remembered.
- §16.6 candidate validation: extraction returns structured candidates
  and the SERVER validates them — a model cannot grant its own memory
  broader visibility or label an unsupported claim confirmed.
- §16.14 content-as-data: retrieved memory is untrusted data; hostile
  stored content cannot carry authority (the same provenance classes
  as Phase 15 handoffs).
- §16.19 preferences: explicit current-task instructions OVERRIDE
  general preferences; application is visible and correctable.
- §16.22 deletion: marker → hidden → cleanup, observable and retryable.
- §16.32 progressive release: collection, retrieval, and automatic
  reuse are independently configurable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.models.admission import epoch_now

SCOPES = ("task", "workflow", "personal", "project", "organization",
          "published")
VERIFICATION_STATUSES = ("confirmed", "inferred", "disputed", "unverified")
SENSITIVITIES = ("normal", "sensitive", "secret_ref")

# §16.4: secret-shaped content is refused to ordinary memory. The
# patterns catch the common credential shapes; the DESIGN rule is that
# only credential-store REFERENCES may be remembered.
_SECRET_PATTERNS = (
    re.compile(r"\b(sk-[A-Za-z0-9]{20,})\b"),                    # API keys
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),                       # AWS keys
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b"),           # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),  # JWTs
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b"
               r"\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class MemoryGovernanceError(ValueError):
    pass


def contains_secret(content: str) -> Optional[str]:
    """§16.4: return the MATCHED pattern kind when the content carries a
    secret-shaped value — the caller refuses persistence/indexing/
    summarization/export of it."""
    for pattern in _SECRET_PATTERNS:
        m = pattern.search(content or "")
        if m:
            return pattern.pattern[:40]
    return None


@dataclass
class MemoryCandidate:
    """§16.6: the structured extraction candidate — the model PROPOSES,
    the server validates."""
    content: str
    memory_class: str                 # the five lifecycle classes
    scope: str = "personal"
    scope_ref: str = ""
    source: str = ""
    explicit: bool = False            # user-stated vs agent-inferred
    expected_lifetime: str = "until_changed"   # until_changed | session | dated
    expires_at: Optional[int] = None
    observed_at: Optional[int] = None
    potential_conflicts: List[str] = field(default_factory=list)
    source_event_id: Optional[str] = None
    sensitivity: str = "normal"
    # Set BY THE SERVER, never by the model:
    verification_status: str = "unverified"
    policy_decision: str = ""         # stored | needs_confirmation | refused
    policy_reason: str = ""


# §16.3: the write policy per class — what may be remembered
# automatically vs what needs the user's confirmation.
AUTO_REMEMBER_CLASSES = {
    # Verified workflow outcomes are evidence-backed episodes.
    "execution_episode": "auto_if_verified",
    # Repeated explicit instructions form a preference the user already
    # stated — no re-asking for low-impact details.
    "preference": "auto_if_explicit",
}
CONFIRM_FIRST_CLASSES = {
    # Consequential personal facts and uncertain interpretations need
    # the user's confirmation before they become retrievable "facts".
    "factual": "confirm_if_inferred",
    "procedure": "confirm_always",     # procedures route through review
    "transient_context": "auto",       # task-scoped, expires by design
}


def decide_write_policy(candidate: MemoryCandidate) -> MemoryCandidate:
    """§16.3/§16.6: the server decides — independent of the extraction
    model. The model cannot label an unsupported claim 'confirmed'; an
    inferred claim stores as 'inferred' at best. The policy decision and
    reason travel with the record (§16.3 acceptance: the platform can
    explain why it stored something)."""
    if contains_secret(candidate.content):
        candidate.policy_decision = "refused"
        candidate.policy_reason = ("secret-shaped content cannot enter "
                                   "ordinary memory (§16.4) — store a "
                                   "credential-store REFERENCE instead")
        return candidate
    if candidate.scope not in SCOPES:
        candidate.policy_decision = "refused"
        candidate.policy_reason = f"unknown scope '{candidate.scope}'"
        return candidate
    if candidate.scope in ("organization", "published") and \
            not candidate.explicit:
        # §16.1: sharing is an authorized operation — an inference can
        # never publish to the organization.
        candidate.policy_decision = "refused"
        candidate.policy_reason = (
            "only EXPLICIT, user-authorized statements may carry "
            "organization/published scope (§16.1)")
        return candidate
    if candidate.memory_class == "factual" and not candidate.explicit:
        candidate.verification_status = "inferred"
        candidate.policy_decision = "needs_confirmation"
        candidate.policy_reason = (
            "an inferred personal fact needs user confirmation before it "
            "is treated as confirmed (§16.3)")
        return candidate
    if candidate.memory_class == "preference" and candidate.explicit:
        candidate.verification_status = "confirmed"
        candidate.policy_decision = "stored"
        candidate.policy_reason = ("explicitly instructed preference — "
                                   "no re-asking (§16.3)")
        return candidate
    if candidate.memory_class == "procedure":
        candidate.policy_decision = "needs_confirmation"
        candidate.policy_reason = (
            "proposed procedures route through reviewed acceptance "
            "(§16.17) — never auto-activated")
        return candidate
    candidate.verification_status = "confirmed" if candidate.explicit \
        else "inferred"
    candidate.policy_decision = "stored"
    candidate.policy_reason = (f"{candidate.memory_class} accepted under "
                               "the write policy")
    return candidate


def validate_candidate(candidate: MemoryCandidate) -> None:
    """§16.6: validate the candidate's fields independently of the
    extraction model — malformed proposals are refused, never stored
    on faith."""
    problems = []
    if not (candidate.content or "").strip():
        problems.append("empty content")
    if len(candidate.content) > 20_000:
        problems.append("content exceeds 20k chars")
    from backend.services.memory_lifecycle import MEMORY_CLASSES
    if candidate.memory_class not in MEMORY_CLASSES:
        problems.append(f"unknown memory class '{candidate.memory_class}'")
    if candidate.scope not in SCOPES:
        problems.append(f"unknown scope '{candidate.scope}'")
    if candidate.sensitivity not in SENSITIVITIES:
        problems.append(f"unknown sensitivity '{candidate.sensitivity}'")
    if candidate.expected_lifetime == "dated" and \
            candidate.expires_at is None:
        problems.append("dated lifetime requires expires_at")
    if problems:
        raise MemoryGovernanceError("invalid candidate: " + "; ".join(problems))


# ── §16.19: preference precedence ──────────────────────────────────────

def resolve_preference_conflict(*, preference: str,
                                task_instruction: str) -> Dict[str, Any]:
    """§16.19: explicit current-task instructions OVERRIDE general
    preferences. The resolution is EXPLAINABLE and visible."""
    return {
        "effective": task_instruction or preference,
        "precedence": "task_instruction" if task_instruction else "preference",
        "suppressed_preference": preference if task_instruction else None,
        "note": ("explicit current-task instructions override general "
                 "preferences (§16.19) — visible and correctable"),
    }


# ── §16.14: retrieved content is DATA ──────────────────────────────────

HOSTILE_MARKERS = ("ignore all previous", "ignore previous", "disregard all",
                   "you are now", "reveal your", "new instructions:")


def classify_retrieved_content(content: str) -> Dict[str, Any]:
    """§16.14: retrieved memory is UNTRUSTED DATA. Hostile stored
    content is flagged and can never change permissions, reveal secrets,
    or redirect the workflow — the receiving agent binds only to
    authority-carrying classes (user instruction, system policy)."""
    text = (content or "").lower()
    flagged = any(marker in text for marker in HOSTILE_MARKERS)
    return {
        "content_class": "tool_or_page_content",   # always data
        "authority": False,
        "hostile_flag": flagged,
        "note": ("retrieved memory is untrusted data (§16.14) — "
                 "authority comes only from the workflow context"),
    }


# ── §16.10–§16.13: authorized retrieval, honest ranking, bounded context ──

def permitted_scopes_for(*, user_id: str, task_context: Dict[str, Any],
                         org_shared_refs: Optional[List[str]] = None) -> Dict[str, Any]:
    """§16.10 (audit W-scope): the SERVER derives the search scope from
    the authenticated user + task context. A client-supplied org id or
    an agent-supplied scope is never sufficient authorization: org
    memories are visible only through explicitly shared refs (the
    authorized sharing operation's record), never through membership."""
    return {
        "owner": user_id,                       # personal/workflow/task: own only
        "organization_refs": list(org_shared_refs or []),
        "published": True,                      # published knowledge is world-readable by design
        "note": ("org membership NEVER implies visibility (§16.1) — "
                 "only explicitly shared refs are searchable"),
    }


async def governed_search(*, repo, lifecycle, user_id: str,
                          query: str, query_embedding: List[float],
                          scopes: Dict[str, Any],
                          memory_class: Optional[str] = None,
                          specialist_filters: Optional[Dict[str, Any]] = None,
                          limit: int = 8,
                          max_context_chars: int = 4000,
                          ) -> Dict[str, Any]:
    """§16.10–16.13: the ONE authorized retrieval path.

    - Filters run BEFORE scoring: owner scope, deletion state, expiry,
      superseded rows, class filter (§16.10/§16.9/§16.4).
    - THREE retrieval methods combine: exact substring, keyword overlap,
      semantic similarity (§16.11) — exact identifiers are not displaced
      by vague similarity.
    - Ranking keeps the signals SEPARATE (§16.12): relevance × freshness
      × verification × conflict status — each reported, no opaque blend.
    - The context is BOUNDED (§16.13): records ≤ limit, total chars ≤
      max_context_chars, status labels preserved verbatim (compression
      never turns 'unverified' into 'confirmed').
    """
    now = epoch_now()
    # ── Candidate filter (authorization + lifecycle BEFORE scoring) ──
    rows = await repo.list_memories(user_id, limit=500)
    # §16.10: ORGANIZATION-scope rows live under their authors' owner
    # keys — the search universe includes them ONLY through the
    # explicitly shared refs (never through membership).
    for ref in scopes.get("organization_refs", []):
        try:
            cursor = await repo.db.execute(
                "SELECT * FROM memories WHERE scope='organization' AND "
                "scope_ref=? AND deletion_state='active' LIMIT 200",
                (ref,))
            import json as _j
            for row in await cursor.fetchall():
                d = dict(row)
                d["metadata"] = _j.loads(d.get("metadata") or "{}")
                d["memory_class"] = d.get("memory_class") or "factual"
                rows.append(d)
        except Exception:
            pass
    candidates = []
    for r in rows:
        if (r.get("deletion_state") or "active") != "active":
            continue   # deletion barrier: requested/hidden rows are dark
        if r.get("superseded_by"):
            continue   # superseded history is inspectable, not retrievable
        if r.get("expires_at") and int(r["expires_at"]) <= now:
            continue   # expired freshness is not served
        if memory_class and r.get("memory_class") != memory_class:
            continue
        if specialist_filters is not None:
            metadata = r.get("metadata") or {}
            if not isinstance(metadata, dict):
                continue
            if (r.get("memory_class") or metadata.get("memory_class")) not in specialist_filters["classes"]:
                continue
            if metadata.get("topic") not in specialist_filters["topics"]:
                continue
        scope = r.get("scope") or "personal"
        if scope == "organization" and \
                r.get("scope_ref") not in scopes.get("organization_refs", []):
            continue   # org rows need an explicit shared ref
        candidates.append(r)

    q = (query or "").strip().lower()
    q_terms = [t for t in re.split(r"\W+", q) if len(t) > 2]

    def _kw_hits(text: str) -> int:
        # §16.11: keyword matching on WORD BOUNDARIES — 'rev' must not
        # match 'reviews' (exact identifiers are not displaced by vague
        # substring luck).
        hits = 0
        for t in q_terms:
            if re.search(rf"\b{re.escape(t)}\b", text):
                hits += 1
        return hits

    # ── Three retrieval methods (§16.11) ───────────────────────────────
    scored = []
    for r in candidates:
        content = (r.get("content") or "")
        c_low = content.lower()
        exact = q in c_low if q else False
        kw_hits = _kw_hits(c_low)
        kw_score = kw_hits / len(q_terms) if q_terms else 0.0
        try:
            import json as _json
            emb = _json.loads(r.get("embedding") or "[]")
        except (ValueError, TypeError):
            emb = []
        sem = 0.0
        if emb and query_embedding:
            from backend.repositories.sqlite.memory_repository import (
                _cosine_similarity)
            sem = max(0.0, _cosine_similarity(emb, query_embedding))
        relevance = 1.0 if exact else max(kw_score, sem)
        if relevance <= 0.0:
            continue
        # ── §16.12: separate quality signals ──────────────────────────
        created = r.get("created_at") or ""
        freshness = 1.0   # preferences stay valid until changed
        if r.get("memory_class") in ("execution_episode", "transient_context"):
            try:
                from datetime import datetime as _dt
                age = now - int(_dt.fromisoformat(str(created).replace(
                    "Z", "+00:00")).timestamp())
                freshness = max(0.0, 1.0 - age / (86_400 * 30))
            except (ValueError, TypeError):
                freshness = 0.5
        verification = {"confirmed": 1.0, "inferred": 0.6,
                        "disputed": 0.2, "unverified": 0.4}.get(
                            r.get("verification_status") or "unverified", 0.4)
        conflict = 0.0 if not r.get("subject_key") else 0.0  # superseded filtered already
        scored.append({
            **r,
            "ranking": {"relevance": round(relevance, 4),
                        "freshness": round(freshness, 4),
                        "verification": verification,
                        "conflict": conflict,
                        "method": ("exact" if exact else
                                   "keyword" if kw_score >= sem else
                                   "semantic")},
        })
    scored.sort(key=lambda x: (x["ranking"]["relevance"] * 0.5
                               + x["ranking"]["verification"] * 0.3
                               + x["ranking"]["freshness"] * 0.2),
                reverse=True)
    picked = scored[:limit]
    # ── §16.13: bounded context — total chars capped, statuses preserved ──
    used = 0
    bounded = []
    for r in picked:
        text = r.get("content") or ""
        if used + len(text) > max_context_chars:
            break
        used += len(text)
        bounded.append({
            "memory_id": r["id"],
            "content": text,
            "memory_class": r.get("memory_class"),
            "status": r.get("verification_status") or "unverified",
            "scope": r.get("scope"),
            "source": r.get("source"),
            "ranking": r["ranking"],
            "content_class": "tool_or_page_content",   # §16.14: data, not authority
        })
    excluded = [{"memory_id": r["id"], "reason":
                 "context budget exhausted"} for r in picked[len(bounded):]]
    return {"results": bounded, "excluded": excluded,
            "context_chars": used,
            "note": ("every result carries its status verbatim — "
                     "compression never upgrades a claim (§16.13)")}


# ── §16.15: specialist task-specific memory access ─────────────────────

SPECIALIST_MEMORY_SCOPE = {
    # Each specialist receives the MINIMUM relevant scope — a validator
    # needs operation specs and validation evidence, not conversations.
    "connector_validator": {"classes": ["factual", "execution_episode"],
                            "topics": ["operation_spec", "validation"]},
    "outcome_verifier": {"classes": ["factual", "execution_episode"],
                         "topics": ["criteria", "evidence"]},
    "resume_specialist": {"classes": ["preference", "factual"],
                          "topics": ["approved_profile_facts"]},
    "recovery_analyst": {"classes": ["execution_episode"],
                         "topics": ["failure", "recovery"]},
}


def specialist_memory_filters(role: str, requested: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Intersect the role ceiling and any narrower grant carried in its contract."""
    maximum = SPECIALIST_MEMORY_SCOPE.get(role, {"classes": [], "topics": []})
    scope = {}
    if requested is not None and not isinstance(requested, dict):
        raise ValueError("Invalid specialist memory scope")
    for field in ("classes", "topics"):
        permitted = maximum[field]
        values = (requested or {}).get(field, permitted)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("Invalid specialist memory filters")
        scope[field] = [v for v in permitted if v in values]
    scope["note"] = ("minimum relevant scope (§16.15) — the child cannot widen it"
                     if scope["classes"] and scope["topics"] else "no memory access granted for this role")
    return scope


# ── §16.32: progressive memory release ─────────────────────────────────

MEMORY_STAGES = ("task_scoped_context", "explicit_facts",
                 "source_backed_retrieval", "verified_execution_history",
                 "reviewed_procedures", "bounded_auto_extraction",
                 "broader_personalization")


def memory_stage_flags(settings: Any = None) -> Dict[str, Any]:
    """§16.32: memory collection, retrieval, and automatic reuse are
    independently configurable; defaults keep the shipped stages on and
    the newer ones opt-in."""
    raw = str(getattr(settings, "MEMORY_STAGES_ENABLED", "") or
              "") if settings is not None else ""
    enabled = {s.strip() for s in raw.split(",") if s.strip()}
    default_on = {"task_scoped_context", "explicit_facts",
                  "source_backed_retrieval", "verified_execution_history"}
    return {"stages": {s: (s in enabled or s in default_on)
                       for s in MEMORY_STAGES},
            "collection_enabled": True,
            "retrieval_enabled": True,
            "automatic_reuse": "bounded_auto_extraction" in enabled}


async def require_memory_stage(stage: str, settings: Any = None) -> None:
    flags = memory_stage_flags(settings)
    if not flags["stages"].get(stage):
        from backend.services.specialist_release import SpecialistReleaseError
        raise SpecialistReleaseError(
            f"The '{stage}' memory stage is not enabled (§16.32)")


# ── §16.25: abstention — "no reliable memory found" is a real answer ───

def retrieval_abstention(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """§16.25: the system returns 'no reliable memory found' instead of
    manufacturing an answer — unverified/disputed-only results and empty
    result sets are abstentions, and the caller knows which."""
    usable = [r for r in results or []
              if (r.get("status") or "unverified") in ("confirmed",)]
    if not results:
        return {"abstained": True, "reason": "no_retrieved_memory",
                "usable": []}
    if not usable:
        return {"abstained": True,
                "reason": "only_unverified_or_disputed_results",
                "usable": usable}
    return {"abstained": False, "reason": "verified_results_available",
            "usable": usable}


# ── §16.26: memory's measured effect on outcomes ──────────────────────

def evaluate_memory_effect(*, baseline: Dict[str, Any],
                           with_memory: Dict[str, Any]) -> Dict[str, Any]:
    """§16.26: compare workflows WITH and WITHOUT memory on the same
    task population. Higher-is-better: verified completion, deliverable
    factuality. Lower-is-better: user corrections, repeated questions,
    context cost, latency, incorrect personalization, stale reliance,
    unauthorized use. Missing measurements stay MISSING."""
    metrics_high = ("verified_completion", "deliverable_factuality")
    metrics_low = ("user_corrections", "repeated_questions",
                   "context_cost", "latency", "incorrect_personalization",
                   "stale_reliance", "unauthorized_use")
    out = {}
    missing = []
    for m in metrics_high + metrics_low:
        b = baseline.get(m)
        w = with_memory.get(m)
        if b is None or w is None:
            missing.append(m)
            continue
        delta = round(w - b, 4)
        better = (delta >= 0) if m in metrics_high else (delta <= 0)
        out[m] = {"baseline": b, "with_memory": w, "delta": delta,
                  "verdict": "improved" if better and delta != 0
                  else "regressed" if delta != 0 else "flat"}
    measured = [m for m in out]
    if not measured:
        case = "unmeasured"
    elif any(out[m]["verdict"] == "regressed" for m in measured):
        case = "tradeoff"
    elif any(out[m]["verdict"] == "improved" for m in measured):
        case = "improvement"
    else:
        case = "no_case"
    return {"case": case, "metrics": out, "unmeasured": missing,
            "note": ("memory must demonstrate measured benefit WITHOUT "
                     "increasing unsupported claims or unauthorized use "
                     "(§16.26)")}
