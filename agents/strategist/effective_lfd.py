"""Leak 01: the clock started before you could act.

The nominal last free day is what the carrier bills against. The effective last
free day is what we argue. The gap is the days the container was discharged but
not actually collectable - buried in a stack, ungrounded, unappointable.

Almost nobody adjusts for this, because it needs an availability timestamp from
a terminal portal nobody checks on day two. Those days push the whole delay
deeper into a convex tier ladder, which is where the money is.

Pure arithmetic. No LLM.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from agents.shared.charges import last_free_day
from agents.shared.models import ClockTerms, Evidence, EventKind, LfdCalculation


def compute_effective_lfd(
    terms: ClockTerms,
    discharged_at: datetime,
    available_at: datetime | None,
    availability_misses: list[Evidence] | None = None,
) -> LfdCalculation:
    """Shift the last free day by the measured ungrounded window.

    Args:
        terms: Demurrage clock terms.
        discharged_at: When the box came off the vessel. Starts the nominal clock.
        available_at: When it first became genuinely collectable. ``None`` means
            still unavailable, so the gap is still open.
        availability_misses: Recorded failed polls. Each is a successful
            observation of an adverse fact and becomes a citation.

    Returns:
        An ``LfdCalculation`` carrying both dates, the gap, and its citations.
    """
    discharge_day = discharged_at.date()
    nominal = last_free_day(terms, discharge_day)

    citations: list[str] = []
    for miss in availability_misses or []:
        if miss.kind in (EventKind.AVAILABILITY_MISS, EventKind.CONTAINER_AVAILABLE):
            citations.append(miss.source_document_id)

    if available_at is None:
        # Still ungrounded. The gap is open, so measure it to now-known extent.
        return LfdCalculation(
            nominal_lfd=nominal,
            effective_lfd=nominal,
            ungrounded_days=0,
            discharged_at=discharged_at,
            available_at=None,
            citations=sorted(set(citations)),
        )

    ungrounded = (available_at.date() - discharge_day).days
    ungrounded = max(0, ungrounded)

    return LfdCalculation(
        nominal_lfd=nominal,
        effective_lfd=nominal + timedelta(days=ungrounded),
        ungrounded_days=ungrounded,
        discharged_at=discharged_at,
        available_at=available_at,
        citations=sorted(set(citations)),
    )


def recoverable_days(calc: LfdCalculation, billed_through: date) -> int:
    """How many billed days the effective-LFD argument would strip out.

    Zero when the container cleared before the effective LFD; otherwise the
    number of days between nominal and effective that were actually billed.
    """
    if not calc.shifted:
        return 0
    if billed_through <= calc.nominal_lfd:
        return 0
    reachable = min(billed_through, calc.effective_lfd)
    return max(0, (reachable - calc.nominal_lfd).days)
