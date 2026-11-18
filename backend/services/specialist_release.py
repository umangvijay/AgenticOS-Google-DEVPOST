"""Progressive specialist release (Phase 15 §15.30) — roles and
workflows enable in STAGES, with role versions and rollback visible.

Stage order (each enables only when the previous has passed its gates):
  read_only_tasks → independent_verification → connector_generation →
  bounded_recovery → approved_writes → browser_workflows →
  learned_selection

An operator can DISABLE a role: delegation refuses, workflow history
and accepted work remain intact (contracts are durable rows — disabling
never rewrites them).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SPECIALIST_STAGES = (
    "read_only_tasks",
    "independent_verification",
    "connector_generation",
    "bounded_recovery",
    "approved_writes",
    "browser_workflows",
    "learned_selection",
)

# The MINIMUM stage each role requires to be delegable.
ROLE_MIN_STAGE = {
    "deliverable_assembler": "connector_generation",
    "provenance_reviewer": "read_only_tasks",
    "document_quality_reviewer": "read_only_tasks",
    "build_verifier": "read_only_tasks",
    "delivery_reconciler": "bounded_recovery",
    "outcome_verifier": "independent_verification",
    "dom_resolver": "read_only_tasks",
    "evaluation_curator": "read_only_tasks",
    "release_verifier": "read_only_tasks",
    "improvement_proposer": "independent_verification",
    "reconciliation": "bounded_recovery",
    "recovery_analyst": "bounded_recovery",
    "connector_validator": "connector_generation",
    "browser_navigator": "browser_workflows",
}


class SpecialistReleaseError(PermissionError):
    pass


# Stages that have ALREADY shipped through their acceptance suites —
# enabled by default (read-only, verification, connector generation,
# bounded recovery). The NEWER stages (approved external writes,
# browser workflows, learned selection) stay opt-in per deployment.
_DEFAULT_ENABLED_STAGES = {
    "read_only_tasks", "independent_verification",
    "connector_generation", "bounded_recovery",
}


def specialist_stage_flags(settings: Any = None) -> Dict[str, Any]:
    """Which release stages are enabled: the ALREADY-SHIPPED stages by
    default, the newer stages only when the deployment lists them
    (SPECIALIST_STAGES_ENABLED). Role versions are visible for
    rollback."""
    enabled_raw = str(getattr(settings, "SPECIALIST_STAGES_ENABLED", "") or
                      "") if settings is not None else ""
    enabled = {s.strip().lower() for s in enabled_raw.split(",") if s.strip()}
    flags = {stage: (stage in enabled or stage in _DEFAULT_ENABLED_STAGES)
             for stage in SPECIALIST_STAGES}
    versions = getattr(settings, "SPECIALIST_ROLE_VERSIONS", {}) or {}
    return {"stages": flags, "role_versions": dict(versions)}


def require_role_stage(role: str, settings: Any = None,
                       disabled_roles: Optional[List[str]] = None) -> None:
    """§15.30: delegation REFUSES a role whose release stage is not
    enabled or that the operator explicitly disabled. Workflow history
    is untouched — this gates NEW delegation only."""
    flags = specialist_stage_flags(settings)
    if role in set(disabled_roles or []):
        raise SpecialistReleaseError(
            f"Specialist role '{role}' is disabled by the operator "
            "(§15.30) — accepted work and history remain intact")
    minimum = ROLE_MIN_STAGE.get(role)
    if minimum is None:
        return   # an unregistered role: the registry layer refuses anyway
    if not flags["stages"].get(minimum):
        raise SpecialistReleaseError(
            f"Specialist role '{role}' requires the '{minimum}' release "
            "stage, which is not enabled on this deployment")
