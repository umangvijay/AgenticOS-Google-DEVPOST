"""Schedule wall-clock computation (§29.19).

A schedule's cron fields are WALL-CLOCK in its declared timezone. The
previous path computed `next_run_at` with croniter over naive UTC, so a
"09:00 in America/New_York" schedule silently meant 09:00 UTC and DST
transitions drifted the wall-clock time the user asked for. All next-run
computation goes through here: the timezone is validated with ZoneInfo
at save time, the cron iterates in the schedule's own timezone, and the
result is stored as UTC (the durable comparison key for due schedules).

Missed-run behavior (defined here, enforced by the worker): a due
schedule fires at most once per pass and next_run_at advances from now,
so a backlog of missed windows collapses into ONE run; a large miss is
surfaced to the user as a notification rather than firing silently.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, Optional

from zoneinfo import ZoneInfo

_UTC = dt_timezone.utc


def validate_timezone(timezone_name: str) -> str:
    """Reject an unknown timezone at admission — an invalid tz must fail
    the request, not silently schedule in UTC."""
    name = (timezone_name or "UTC").strip() or "UTC"
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"unknown timezone: {name}")
    return name


def next_run_at(cron_expression: str, timezone_name: str,
                after: Optional[datetime] = None) -> Optional[str]:
    """The next occurrence of the cron in the schedule's timezone,
    returned as a UTC ISO string. DST transitions are honored because
    croniter iterates over aware wall-clock datetimes in the zone."""
    try:
        from croniter import croniter
        zone = ZoneInfo(validate_timezone(timezone_name))
        base = after.astimezone(zone) if after is not None \
            else datetime.now(zone)
        if base.tzinfo is None:
            base = base.replace(tzinfo=zone)
        cron = croniter(cron_expression, base)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=zone)
        return nxt.astimezone(_UTC).isoformat()
    except ValueError:
        raise
    except Exception:
        return None


# ── §29.19 revision: gap/fold policies ────────────────────────────────
# A wall-clock occurrence may not exist (spring gap) or may exist twice
# (fall fold). The persisted policy decides, deterministically:
#   skip        — a nonexistent local time is SKIPPED; the next legal
#                 occurrence is used (never a shifted instant)
#   first       — the FIRST offset interpretation (fold=0)
#   second      — the SECOND offset interpretation (fold=1)
MISSED_RUN_POLICIES = ("skip", "first", "second", "coalesce_one")


def resolve_occurrence(cron_expression: str, timezone_name: str,
                       after: Optional[datetime] = None,
                       gap_policy: str = "skip",
                       fold_policy: str = "first") -> Optional[Dict[str, Any]]:
    """Resolve the next occurrence into its canonical UTC instant AND the
    logical LOCAL wall-clock occurrence (what the user wrote), with the
    gap/fold policies applied. croniter iterates on the NAIVE wall clock
    (so a spring-gap wall time is actually visited, not silently
    normalized), and localization is deterministic:

    - localize(naive, fold=0) round-trips → legal, use it (fold=0 IS the
      first interpretation for folds too);
    - else localize(naive, fold=1) round-trips → a fall fold; the
      fold_policy chooses first/second;
    - else nonexistent (spring gap) → "skip" advances the WALL CLOCK to
      the next legal occurrence; "first"/"second" pin the pre-transition
      offset (the historical interpretation).

    Returns {utc_instant, local_occurrence, zone, gap_shifted,
    fold_ambiguous} or None when the cron cannot resolve."""
    try:
        from croniter import croniter
        zone = ZoneInfo(validate_timezone(timezone_name))
        # The naive wall-clock cursor: what the user actually wrote.
        wall_base = after.astimezone(zone).replace(tzinfo=None) \
            if after is not None else datetime.now().astimezone(zone) \
            .replace(tzinfo=None)
        cron = croniter(cron_expression, wall_base)

        def localize(naive: datetime):
            """(utc_instant, ambiguous) for one wall-clock candidate.
            The fold is honored by the AWARE→UTC conversion (PEP 495),
            not by naive→aware localization — so ambiguity is detected
            by comparing the two UTC instants, and a nonexistent wall
            time by its round-trip shifting."""
            utc_first = naive.replace(fold=0, tzinfo=zone).astimezone(_UTC)
            utc_second = naive.replace(fold=1, tzinfo=zone).astimezone(_UTC)
            if utc_first.astimezone(zone).replace(tzinfo=None) != naive:
                return None, False  # nonexistent wall time (spring gap)
            return (utc_first if fold_policy != "second" else utc_second), \
                (utc_first != utc_second)

        gap_shifted = False
        naive = cron.get_next(datetime)
        for _ in range(24 * 60 * 2):  # bounded: at most two days of minutes
            utc_dt, fold_ambiguous = localize(naive)
            if utc_dt is not None:
                return {
                    "utc_instant": utc_dt.isoformat(),
                    "local_occurrence": naive.isoformat(),
                    "zone": timezone_name,
                    "gap_shifted": gap_shifted,
                    "fold_ambiguous": fold_ambiguous,
                }
            if gap_policy == "skip":
                gap_shifted = True
                naive = cron.get_next(datetime)
                continue
            # "first"/"second" pin the offset interpretation of the
            # nonexistent wall time (the historical reading).
            fold = 1 if gap_policy == "second" else 0
            aware = naive.replace(fold=fold, tzinfo=zone)
            return {
                "utc_instant": aware.astimezone(_UTC).isoformat(),
                "local_occurrence": naive.isoformat(),
                "zone": timezone_name,
                "gap_shifted": False,
                "fold_ambiguous": False,
            }
        return None
    except ValueError:
        raise
    except Exception:
        return None
