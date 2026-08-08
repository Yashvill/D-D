"""Tier arithmetic. Shared by the Strategist (forecast) and the Auditor (recompute).

Deliberately not an LLM. These are convex tier ladders over known dates: the
answer is arithmetic, and a judge asking "how do you know the agent didn't
hallucinate the charge?" deserves a better answer than "we prompted it well".

The convexity is the whole reason early intervention beats late defence. Two
days at the start of a delay might cost $400; the same two days at the end of a
nine-day delay cost $900 or more.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from agents.shared.models import ClockTerms

WEEKEND = (5, 6)  # Saturday, Sunday


def billable_days(
    start: date,
    end: date,
    *,
    counts_weekends: bool = True,
    holidays: frozenset[date] | None = None,
    counts_holidays: bool = True,
) -> list[date]:
    """Days in ``(start, end]`` that count against a clock.

    ``start`` is the last free day and is excluded; ``end`` is the day the clock
    stopped and is included. Weekend and holiday handling is per contract, and
    getting it wrong in the billing party's favour is a routine invoice defect.
    """
    if end <= start:
        return []

    holidays = holidays or frozenset()
    out: list[date] = []
    cursor = start + timedelta(days=1)

    while cursor <= end:
        skip_weekend = not counts_weekends and cursor.weekday() in WEEKEND
        skip_holiday = not counts_holidays and cursor in holidays
        if not (skip_weekend or skip_holiday):
            out.append(cursor)
        cursor += timedelta(days=1)

    return out


def charge_for_period(
    terms: ClockTerms,
    clock_start: date,
    clock_end: date,
    *,
    holidays: frozenset[date] | None = None,
) -> tuple[Decimal, list[tuple[date, int, Decimal]]]:
    """Total charge plus a per-day breakdown.

    Args:
        terms: Free time and tier table for one charge type.
        clock_start: The day the clock started (discharge, or gate-out).
        clock_end: The day the clock stopped.

    Returns:
        ``(total, [(date, billed_day_index, rate), ...])``. The breakdown is what
        makes a dispute checkable line by line.
    """
    lfd = last_free_day(terms, clock_start)
    days = billable_days(
        lfd,
        clock_end,
        counts_weekends=terms.counts_weekends,
        holidays=holidays,
        counts_holidays=terms.counts_holidays,
    )

    total = Decimal("0")
    breakdown: list[tuple[date, int, Decimal]] = []

    for index, day in enumerate(days, start=1):
        rate = terms.rate_for(index)
        total += rate
        breakdown.append((day, index, rate))

    return total, breakdown


def last_free_day(terms: ClockTerms, clock_start: date) -> date:
    """The final day before charges accrue.

    The single most important date in the process, and the one most often
    computed wrongly.
    """
    return clock_start + timedelta(days=terms.free_days)


def project_charge(
    terms: ClockTerms,
    clock_start: date,
    as_of: date,
    projected_end: date,
    *,
    holidays: frozenset[date] | None = None,
) -> tuple[Decimal, Decimal]:
    """Split a projection into what has accrued and what is still forecast.

    Returns ``(accrued_to_date, projected_total)``.
    """
    accrued, _ = charge_for_period(terms, clock_start, as_of, holidays=holidays)
    projected, _ = charge_for_period(terms, clock_start, projected_end, holidays=holidays)
    return accrued, projected


def marginal_cost_of_day(
    terms: ClockTerms,
    clock_start: date,
    current_end: date,
    *,
    holidays: frozenset[date] | None = None,
) -> Decimal:
    """What one more day costs from here.

    This is the number that should drive intervention decisions, not the
    average. Under a convex ladder the marginal day is always the expensive one.
    """
    now, _ = charge_for_period(terms, clock_start, current_end, holidays=holidays)
    later, _ = charge_for_period(
        terms, clock_start, current_end + timedelta(days=1), holidays=holidays
    )
    return later - now
