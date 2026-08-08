"""Drayage booking, with the compensator that keeps the agent honest.

This is what stops an autonomous agent creating phantom bookings and no-show fees
in real carrier systems. Every action with a real-world side effect registers a
compensator before the next step runs - required, not optional.

The ordering matters: book the trucker, then try for a slot. No slot means the
booking must be unwound, or the importer eats a no-show fee the agent itself
created.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from agents.shared.models import Booking

log = logging.getLogger(__name__)


def book_drayage(
    container_id: str,
    *,
    provider: str,
    slot_at: datetime,
    cost_usd: Decimal = Decimal("0"),
    expedited: bool = False,
) -> Booking:
    """Book a drayage move. Mocked for the demo."""
    kind = "drayage_expedited" if expedited else "drayage"
    booking = Booking(
        booking_id=f"dray::{container_id}::{slot_at.isoformat(timespec='seconds')}",
        kind=kind,
        slot_at=slot_at,
        provider=provider,
        cost_usd=cost_usd,
    )
    log.info("booked %s with %s for %s", kind, provider, slot_at)
    return booking


def cancel_drayage(booking: Booking) -> str:
    """Saga compensator: unwind a drayage booking.

    Called in reverse order when a later step fails - customs re-holds the box,
    or no appointment slot materialises.
    """
    log.info("cancelling drayage %s", booking.booking_id)
    return f"cancelled::{booking.booking_id}"


def book_empty_return(
    container_id: str,
    *,
    depot: str,
    slot_at: datetime,
) -> Booking:
    """The decisive action of the entire system.

    Costs nothing and prevents the entire second half of the bill. It is not a
    clever optimisation - it is simply the consequence of something still
    watching one day after gate-out, when the human process has already filed the
    container as done.
    """
    booking = Booking(
        booking_id=f"empty::{container_id}::{slot_at.date().isoformat()}",
        kind="empty_return",
        slot_at=slot_at,
        provider=depot,
        cost_usd=Decimal("0"),
    )
    log.info("booked empty return for %s at %s on %s", container_id, depot, slot_at)
    return booking


def cancel_empty_return(booking: Booking) -> str:
    """Saga compensator for an empty-return slot."""
    log.info("cancelling empty return %s", booking.booking_id)
    return f"cancelled::{booking.booking_id}"


def request_free_time_extension(
    container_id: str,
    *,
    carrier: str,
    days: int,
    reasoning: str,
) -> dict[str, str]:
    """Ask for more free time with a reasoned case.

    Bounded mandate: the agent may *request* an extension. It may never agree to
    amended contract terms.
    """
    log.info("requesting %dd extension from %s for %s", days, carrier, container_id)
    return {
        "container_id": container_id,
        "carrier": carrier,
        "days_requested": str(days),
        "reasoning": reasoning,
        "status": "submitted",
    }
