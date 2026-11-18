"""Trusted principals (Phase 13 §13.15).

The authority boundary is a TRUSTED PRINCIPAL — an identity created from
authenticated credentials (or a trusted system component), carrying its
kind and capabilities — never an actor-name prefix. String actors are
still accepted for backward compatibility and parsed CONSERVATIVELY
("agent:*" → an agent principal with no authority beyond proposing);
HTTP routes construct principals from the authenticated user, and
service entry points enforce the SAME restrictions (an agent principal
cannot grade, approve, promote, or roll back no matter which surface it
arrives through).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Union

# Capabilities (§13.4 separation): propose / evaluate / approve /
# activate / stop / rollback.
CAP_PROPOSE = "propose"
CAP_EVALUATE = "evaluate"
CAP_APPROVE = "approve"
CAP_ACTIVATE = "activate"
CAP_ROLLBACK = "rollback"

_OPERATOR_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_PROPOSE, CAP_EVALUATE, CAP_APPROVE, CAP_ACTIVATE, CAP_ROLLBACK})
_AGENT_CAPABILITIES: FrozenSet[str] = frozenset({CAP_PROPOSE})


@dataclass(frozen=True)
class Principal:
    kind: str                       # "operator" | "agent" | "system"
    id: str
    capabilities: FrozenSet[str] = field(default_factory=frozenset)

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def as_actor(self) -> str:
        return f"{self.kind}:{self.id}"


def operator_principal(user_id: str) -> Principal:
    """From an AUTHENTICATED user (the HTTP layer's role check decides
    admin-ness; this grants the operator capability set)."""
    return Principal(kind="operator", id=str(user_id),
                     capabilities=_OPERATOR_CAPABILITIES)


def agent_principal(name: str) -> Principal:
    """A trusted agent identity — proposes only, forever."""
    return Principal(kind="agent", id=str(name),
                     capabilities=_AGENT_CAPABILITIES)


def system_principal(name: str = "system") -> Principal:
    return Principal(kind="system", id=name, capabilities=_OPERATOR_CAPABILITIES)


def principal_from_actor(actor: Union[str, Principal, None]) -> Optional[Principal]:
    """Conservative parsing for legacy string actors: anything identifying
    as an agent gets agent capabilities (propose only); everything else
    is treated as an operator — the HTTP layer's admin check remains the
    outer gate for user-facing surfaces."""
    if actor is None:
        return None
    if isinstance(actor, Principal):
        return actor
    text = str(actor).strip().lower()
    if text.startswith("agent:"):
        return agent_principal(text.split("agent:", 1)[1])
    if text.startswith("operator:"):
        return operator_principal(text.split("operator:", 1)[1])
    if text.startswith("system:"):
        return system_principal(text.split("system:", 1)[1])
    # Unknown string format: assume operator (HTTP routes gate admin-ness).
    return operator_principal(str(actor))
