"""Deliverable assurance and factual provenance (Phase 17 §17.3/§17.7).

Every significant fact in a deliverable traces to a SUPPLIED source;
unsupported facts are flagged, never silently passed. Deliverables carry
content checksums and the source revisions that produced them.

- claim types: employer | date | qualification | achievement | skill |
  numeric — the resume fact families the audit called out (G08).
- provenance is computed against the SOURCE TEXT the user supplied:
  a fact that appears (case-insensitive, whitespace-normalized) in the
  sources is SUPPORTED; anything else is UNSUPPORTED and flagged.
- rephrasing is allowed; ADDING a factual claim is not — the flag
  distinguishes them (§12.27: distinguish rephrasing from adding facts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("provenance")

CLAIM_TYPES = ("employer", "date", "qualification", "achievement",
               "skill", "numeric")

# Field-path prefixes in resume payloads mapped to claim types.
_FIELD_CLAIM_MAP = {
    "company": "employer", "employer": "employer", "organization": "employer",
    "date": "date", "start_date": "date", "end_date": "date",
    "graduation": "date", "period": "date",
    "degree": "qualification", "qualification": "qualification",
    "certification": "qualification", "school": "qualification",
    "university": "qualification",
    "achievement": "achievement", "accomplishment": "achievement",
    "skill": "skill", "skills": "skill",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def claim_type_for(field_path: str) -> str:
    lower = str(field_path or "").lower()
    for key, claim in _FIELD_CLAIM_MAP.items():
        if key in lower:
            return claim
    if re.search(r"(20\d{2}|19\d{2})", lower) or "date" in lower:
        return "date"
    return "achievement"


@dataclass
class FactProvenance:
    field_path: str
    value: str
    claim_type: str
    supported: bool
    source_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"field_path": self.field_path, "value": self.value,
                "claim_type": self.claim_type, "supported": self.supported,
                "source_refs": self.source_refs}


@dataclass
class ProvenanceReport:
    facts: List[FactProvenance] = field(default_factory=list)
    source_revisions: List[str] = field(default_factory=list)

    @property
    def unsupported(self) -> List[FactProvenance]:
        return [f for f in self.facts if not f.supported]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_facts": len(self.facts),
            "supported": len(self.facts) - len(self.unsupported),
            "unsupported": [f.to_dict() for f in self.unsupported],
            "facts": [f.to_dict() for f in self.facts],
            "source_revisions": list(self.source_revisions),
        }


def _walk(value: Any, path: str = "") -> List[tuple]:
    """Flatten a nested payload into (field_path, scalar_value) pairs."""
    out: List[tuple] = []
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{path}.{k}" if path else str(k)
            out.extend(_walk(v, child))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.extend(_walk(v, f"{path}[{i}]"))
    elif isinstance(value, str) and value.strip():
        out.append((path, value.strip()))
    return out


def track_provenance(deliverable_data: Any,
                     source_documents: Dict[str, str]) -> ProvenanceReport:
    """§17.3: connect significant facts to supplied sources. Each scalar
    leaf in the deliverable is checked against EVERY source document —
    supported facts cite their sources; the rest are flagged."""
    normalized_sources = {name: _normalize(text)
                          for name, text in (source_documents or {}).items()}
    report = ProvenanceReport(
        source_revisions=[f"{name}:{hashlib.sha256(_normalize(text).encode()).hexdigest()[:12]}"
                          for name, text in (source_documents or {}).items()])
    _NUM_RE = re.compile(r"\d+(?:[.,]\d+)?%?")

    def _numeric_contradiction(value_norm: str, norm: str) -> bool:
        """§17.11 (finding E): changed quantities, dates, and negations
        are CONTRADICTIONS even when the word overlap looks high —
        'revenue increased 10%' vs 'revenue increased 90%' is not a
        paraphrase. Every number in the claim must appear in the source
        (when the claim carries numbers)."""
        claim_nums = _NUM_RE.findall(value_norm)
        if not claim_nums:
            return False
        source_nums = set(_NUM_RE.findall(norm))
        return any(n not in source_nums for n in claim_nums)

    def _negation_contradiction(value_norm: str, norm: str) -> bool:
        """Negation flips meaning: 'not certified' vs 'certified'."""
        negations = ("not ", "never ", "no ")
        for n in negations:
            if n in value_norm and n not in norm:
                return True
            if n in norm and n not in value_norm:
                return True
        return False

    def _supported(value_norm: str) -> List[str]:
        """A fact is supported when its content appears in a source:
        exact containment (verbatim or trivially normalized) OR ≥80% of
        its significant words appear WITH NO numeric or negation
        contradiction (§17.11) — rephrasing stays supported while
        ADDED claims (new words), changed quantities, and flipped
        negations do not."""
        if not value_norm:
            return []
        refs = [name for name, norm in normalized_sources.items()
                if value_norm in norm]
        if refs:
            return refs
        words = [w for w in re.findall(r"[a-z0-9]{3,}", value_norm)]
        if not words:
            return []
        refs = []
        for name, norm in normalized_sources.items():
            hits = sum(1 for w in words if w in norm)
            if words and hits / len(words) >= 0.8:
                if _numeric_contradiction(value_norm, norm):
                    continue   # the quantity changed — a contradiction
                if _negation_contradiction(value_norm, norm):
                    continue   # the meaning flipped
                refs.append(name)
        return refs

    for path, value in _walk(deliverable_data):
        claim_type = claim_type_for(path)
        refs = _supported(_normalize(value))
        report.facts.append(FactProvenance(
            field_path=path, value=value[:300], claim_type=claim_type,
            supported=bool(refs), source_refs=refs))
    return report


def deliverable_checksum(content: Any) -> str:
    """§17.7: the deliverable's content checksum — versioned comparisons
    reference this, never a mutable path."""
    if isinstance(content, (dict, list)):
        blob = json.dumps(content, sort_keys=True, default=str)
    else:
        blob = str(content or "")
    return hashlib.sha256(blob.encode()).hexdigest()
