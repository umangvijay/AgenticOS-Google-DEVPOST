"""Routing action catalog (Phase 14 §14.9) — the DECLARED set of
selectable actions with their availability, so eligibility (§14.10) is
a first-class filter rather than an accident of string lists.

The catalog is the ONE source of candidate models:
- Each action carries its provider, availability (the provider is
  configured AND the model is on the deployment's allow-list), and the
  tier rule (flash-tier only for routine work — never Pro/Reasoning).
- `eligible_actions()` applies §14.10: availability first, then the
  caller's explicit constraints (a user-pinned model excludes
  everything else; a model the server has not enabled is refused, not
  silently swapped).
- The contextual policy NEVER sees an ineligible action — exploration
  cannot discover one (§14.3/§14.10).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class RoutingAction:
    """One selectable action in the routing catalog."""
    name: str
    provider: str                 # gemini | groq | huggingface
    available: bool               # provider configured + allow-listed
    tier: str                     # flash | explicit
    reason_unavailable: str = ""  # WHY it is filtered (visible, not silent)


class RoutingCatalogError(ValueError):
    pass


def catalog_actions(*, allowed_models: List[str],
                    primary_model: str,
                    flash_models: List[str],
                    provider: str = "gemini") -> List[RoutingAction]:
    """Build the catalog from the deployment's configuration. Every
    FLASH model appears; availability marks the allow-listed ones —
    the filtering is VISIBLE (each unavailable action says why)."""
    allowed = [m.strip() for m in allowed_models if m.strip()]
    allowed_set = set(allowed)
    # The catalog lists primary + flash tier FIRST (routing order), then
    # any other allow-listed model (e.g. an explicitly enabled pro tier)
    # so the catalog covers EVERYTHING the deployment can run.
    ordered: List[str] = []
    for m in [primary_model, *flash_models, *allowed]:
        if m and m not in ordered:
            ordered.append(m)
    actions = []
    for name in ordered:
        available = name in allowed
        actions.append(RoutingAction(
            name=name, provider=provider, available=available,
            tier="flash" if name in flash_models and name != primary_model
            else "explicit",
            reason_unavailable="" if available
            else "not on the deployment's model allow-list"))
    return actions


def eligible_actions(actions: List[RoutingAction], *,
                     preferred: Optional[str] = None,
                     provider_overrides: Optional[List[str]] = None) -> List[str]:
    """§14.10: the eligible set — available actions only, in catalog
    order. An explicit (user-pinned) action collapses the set to just
    it; a pinned-but-ineligible action is a REFUSAL, never a silent
    substitution (the user's choice must not silently run elsewhere)."""
    if provider_overrides is not None:
        # A single-provider deployment: non-listed actions are filtered.
        actions = [a for a in actions if a.provider in provider_overrides]
    available = [a for a in actions if a.available]
    if preferred:
        if not any(a.name == preferred and a.available for a in available):
            raise RoutingCatalogError(
                "Selected model is not enabled on this server")
        return [preferred]
    return [a.name for a in available]
