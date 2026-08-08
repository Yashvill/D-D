"""Terminal appointment scanning.

The most valuable by-product in the entire system. Every failed attempt is
written to history with a timestamp, and those records are the strongest dispute
claim available - because the billing party's own invoice will certify that its
performance did not contribute to the charge.

The critical design rule, and the one most implementations get wrong:

    A failed poll is NOT an error.

It is a successful observation of an adverse fact. It belongs in the evidence
list, not the retry log. Modelling it as an exception makes Temporal retry it
away and destroys the evidence you were trying to capture.

Rate limiting belongs at the worker level - one task queue with a concurrency
cap gives fleet-wide politeness toward the terminal API with no coordination
between the ten thousand workflows doing it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from agents.shared.models import (
    AppointmentAttempt,
    Booking,
    Evidence,
    EventKind,
)

log = logging.getLogger(__name__)


class SlotUnavailable(Exception):
    """Raised only to unwind a saga, never to signal a retryable failure.

    The evidence is recorded before this is raised.
    """

    def __init__(self, attempt: AppointmentAttempt):
        self.attempt = attempt
        super().__init__(f"no slot at {attempt.terminal} as of {attempt.attempted_at}")


def reserve_appointment(
    container_id: str,
    terminal: str,
    *,
    attempted_at: datetime,
    slot_available: bool,
    slot_at: datetime | None = None,
) -> AppointmentAttempt:
    """Attempt one slot reservation.

    Returns an attempt record either way. ``slot_at is None`` means the terminal
    had nothing - a fact, recorded, not an exception.

    ``slot_available`` is the mock seam; in production this is the portal
    response.
    """
    reference = f"appt-attempt::{container_id}::{attempted_at.isoformat(timespec='seconds')}"

    if not slot_available:
        log.info("no slot at %s for %s - recording as evidence", terminal, container_id)
        return AppointmentAttempt(
            attempted_at=attempted_at,
            terminal=terminal,
            slot_at=None,
            reference=reference,
        )

    return AppointmentAttempt(
        attempted_at=attempted_at,
        terminal=terminal,
        slot_at=slot_at or attempted_at + timedelta(hours=4),
        reference=reference,
    )


def attempt_to_evidence(attempt: AppointmentAttempt, container_id: str) -> Evidence:
    """Convert an attempt into citable evidence.

    Misses become leak-03 evidence while they are still provable. Evidence
    perishes faster than the invoice arrives, which is the whole reason this is
    captured at the moment of trying.
    """
    if attempt.succeeded:
        return Evidence(
            kind=EventKind.APPOINTMENT_BOOKED,
            occurred_at=attempt.attempted_at,
            source_system="terminal_portal",
            source_document_id=attempt.reference,
            summary=f"appointment secured at {attempt.terminal}",
            detail={
                "container_id": container_id,
                "slot_at": attempt.slot_at.isoformat(),  # type: ignore[union-attr]
            },
        )

    return Evidence(
        kind=EventKind.APPOINTMENT_UNAVAILABLE,
        occurred_at=attempt.attempted_at,
        source_system="terminal_portal",
        source_document_id=attempt.reference,
        summary=f"no appointment slots offered by {attempt.terminal}",
        detail={"container_id": container_id, "terminal": attempt.terminal},
    )


def release_appointment(booking: Booking) -> str:
    """Saga compensator.

    A slot the agent cannot use is released automatically rather than abandoned.
    Human dispatchers book defensively and abandon slots, which destroys terminal
    capacity on exactly the days it was rationed. This is a direct, mechanical
    improvement to the terminal's effective capacity that falls out of a design
    decision made for a completely different reason.
    """
    log.info("releasing appointment %s", booking.booking_id)
    return f"released::{booking.booking_id}"
