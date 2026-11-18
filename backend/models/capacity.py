"""Capacity-lease domain model re-exports (Phase 3 §3.11).

The model lives in backend/models/admission.py beside the admission decision
types — leases ARE admission over concurrent resources, and one import
surface keeps the §3.2 control inventory together. This module exists so
capacity code can depend on `backend.models.capacity` (the layout proposed
in the Phase 3 plan) without duplicating definitions.
"""
from backend.models.admission import (  # noqa: F401
    CapacityError,
    CapacityExhaustedError,
    CapacityLease,
    LeaseConflictError,
    LeaseState,
    ResourceClass,
    epoch_now,
    new_lease_id,
)
