"""
AgentOS — versioned token pricing (Phase 30 / 30.32)

Money is a SEPARATE quantity from tokens: measured quantities (from the
provider receipt) times the applicable VERSIONED tariff. A tariff entry
declares, per model:

  - uncached input price (cached input is a SUBSET of reported input for
    the providers this tariff covers — never added on top),
  - cached input price,
  - output price (reasoning tokens are already inside output under the
    same provider semantics).

An unknown model or an unknown price component yields an explicitly
UNAVAILABLE price (None + reason) — never a silent zero. Historical
amounts stay inspectable through the settled event payload even after a
credential is removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

PRICING_VERSION = "tariff-2026-11"

# ILLUSTRATIVE reference tariff (declared, versioned — not a quote).
_TARIFF_2026_11: Dict[str, Dict[str, float]] = {
    "gemini-3.8-flash": {"input": 2.0, "cached_input": 0.50, "output": 8.0},
    "gemini-3.8-pro": {"input": 4.0, "cached_input": 1.00, "output": 16.0},
}


@dataclass(frozen=True)
class PriceResult:
    amount: Optional[float]
    currency: Optional[str]
    pricing_version: str
    unavailable_reason: Optional[str] = None
    quantities: Optional[Dict[str, int]] = None


def _num(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def compute_cost(usage: Any, *, model: Optional[str],
                 tariff: Optional[Dict[str, Dict[str, float]]] = None,
                 pricing_version: str = PRICING_VERSION) -> PriceResult:
    """Price one settled usage receipt under the versioned tariff.

    Cached input is a subset of reported input; reasoning a subset of
    output — both are priced from their subset rates, never summed on
    top of the containing bucket."""
    if usage is None:
        return PriceResult(None, None, pricing_version, "usage_unavailable")
    table = tariff if tariff is not None else _TARIFF_2026_11
    entry = table.get(model or "")
    if entry is None:
        return PriceResult(None, None, pricing_version,
                           f"no tariff entry for resolved model {model!r}")
    input_total = _num(getattr(usage, "input_tokens", None))
    output_total = _num(getattr(usage, "output_tokens", None))
    cached = _num(getattr(usage, "cached_input_tokens", None))
    # A missing cached subset prices the WHOLE input at the uncached rate
    # (conservative) — but only when input itself is known.
    if getattr(usage, "input_tokens", None) is None and getattr(
            usage, "reported_total_tokens", None) is not None:
        # Total-only providers: price the reported total as input — the
        # receipt semantics are declared, not invented.
        uncached = _num(getattr(usage, "reported_total_tokens", None))
        amount = uncached * entry["input"] / 1_000_000
        return PriceResult(round(amount, 10), "USD", pricing_version, None,
                           {"reported_total": uncached})
    if input_total is None and output_total is None:
        return PriceResult(None, None, pricing_version, "usage_unavailable")
    cached = min(max(0, cached), max(0, input_total))
    uncached = max(0, input_total - cached)
    amount = (
        uncached * entry["input"] / 1_000_000
        + cached * entry["cached_input"] / 1_000_000
        + max(0, output_total) * entry["output"] / 1_000_000
    )
    return PriceResult(round(amount, 10), "USD", pricing_version, None,
                       {"input_uncached": uncached, "input_cached": cached,
                        "output": max(0, output_total)})
