"""DetentionArc: the equipment clock. Days 20-31.

The only arc where the agent changes the outcome rather than documenting it.
Everything here is prevention, and prevention is worth more per dollar than
recovery because it carries no dispute risk at all.

The decisive action happens one day after gate-out, before the cargo is even
unloaded, and it costs nothing. It is not a clever optimisation - it is simply
the consequence of something still watching after the human process has filed
the container as done.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agents.shared.models import (
        AdvisoryClassification,
        Booking,
        ChargeType,
        ContainerInput,
        ContractTerms,
        Evidence,
        EventKind,
        RiskLevel,
    )
    from temporal_backend.activities import registry as act

def _now() -> datetime:
    """Workflow time as tz-naive UTC.

    ``workflow.now()`` returns tz-aware UTC in this SDK version while the domain
    models and inbound event datetimes are tz-naive; comparing the two raises
    ``can't compare offset-naive and offset-aware``. Normalise at the source.
    """
    return workflow.now().replace(tzinfo=None)


IO_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=6,
)
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)

SHORT = timedelta(seconds=30)
LONG = timedelta(minutes=5)
RETURN_WATCH_INTERVAL = timedelta(hours=12)

# How soon after gate-out to price the second clock. One day, deliberately:
# before the cargo is stripped and long before any restriction exists.
DECISION_DELAY = timedelta(days=1)


class DetentionResult(BaseModel):
    container_id: str
    evidence: list[Evidence] = Field(default_factory=list)
    detention_days: int = 0
    chassis_days: int = 0
    accrued_usd: Decimal = Decimal("0")
    prevented_usd: Decimal = Decimal("0")
    return_slot: datetime | None = None
    empty_returned_at: datetime | None = None
    near_miss: bool = False


class DetentionInput(BaseModel):
    container: ContainerInput
    terms: ContractTerms
    gated_out_at: datetime
    auto_approve_limit_usd: Decimal = Decimal("250")
    depot_restriction_prior: float = 0.0


@workflow.defn
class DetentionArc:
    """The equipment clock, from gate-out to empty accepted."""

    def __init__(self) -> None:
        self.evidence: list[Evidence] = []
        self.return_slot: datetime | None = None
        self.return_booking: Booking | None = None
        self.cargo_stripped_at: datetime | None = None
        self.empty_returned_at: datetime | None = None
        self.restriction: AdvisoryClassification | None = None
        self.near_miss = False
        self.approvals: set[str] = set()
        self.spend_usd = Decimal("0")
        self.prevented_usd = Decimal("0")
        self.risk = RiskLevel.PENDING
        self.reassess_now = False

    # ---------------- signals ----------------

    @workflow.signal
    def cargo_stripped(self, at: datetime, source_document_id: str) -> None:
        """Confirms the box is empty and the booked slot is still valid.

        In the unmanaged process this is the moment the container disappears from
        everyone's mental model.
        """
        self.cargo_stripped_at = at
        self.evidence.append(
            Evidence(
                kind=EventKind.CARGO_STRIPPED,
                occurred_at=at,
                source_system="warehouse",
                source_document_id=source_document_id,
                summary="cargo unloaded; container now empty",
            )
        )
        self.reassess_now = True

    @workflow.signal
    def carrier_advisory(self, advisory_text: str, source_document_id: str, at: datetime) -> None:
        self._pending_advisory = (advisory_text, source_document_id, at)
        self.reassess_now = True

    @workflow.signal
    def empty_returned(self, at: datetime, source_document_id: str) -> None:
        """Arriving is not enough - a turned-away trucker does not stop the clock.

        This signal means *accepted*.
        """
        self.empty_returned_at = at
        self.evidence.append(
            Evidence(
                kind=EventKind.EMPTY_RETURNED,
                occurred_at=at,
                source_system="depot",
                source_document_id=source_document_id,
                summary="empty container accepted at depot",
            )
        )

    @workflow.signal
    def approve(self, action: str) -> None:
        self.approvals.add(action)

    # ---------------- queries ----------------

    @workflow.query
    def state(self) -> dict:
        return {
            "risk": self.risk.value,
            "return_slot": self.return_slot.isoformat() if self.return_slot else None,
            "cargo_stripped_at": (
                self.cargo_stripped_at.isoformat() if self.cargo_stripped_at else None
            ),
            "empty_returned_at": (
                self.empty_returned_at.isoformat() if self.empty_returned_at else None
            ),
            "detention_days": self._detention_days,
            "prevented_usd": str(self.prevented_usd),
            "spend_usd": str(self.spend_usd),
            "near_miss": self.near_miss,
            "restriction_matched": bool(self.restriction and self.restriction.affects_this_container),
            "evidence_count": len(self.evidence),
        }

    # ---------------- run ----------------

    @workflow.run
    async def run(self, inp: DetentionInput) -> DetentionResult:
        self._pending_advisory: tuple | None = None
        self._detention_days = 0

        container = inp.container
        terms = inp.terms
        gated_out_day = inp.gated_out_at.date()

        detention_free = terms.clock(ChargeType.DETENTION).free_days
        detention_due = gated_out_day + timedelta(days=detention_free)

        # Wait a day, then price the second clock. Do not wait for stripping -
        # the whole point is to act before anyone else is paying attention.
        await self._sleep_for(DECISION_DELAY)

        if self.empty_returned_at is None:
            await self._decide_return(container, terms, gated_out_day, detention_due, inp)

        # Watch until the empty is accepted, handling advisories as they land.
        await self._watch_until_returned(container, inp, detention_due)

        returned_day = (
            self.empty_returned_at.date() if self.empty_returned_at else detention_due
        )
        accrued, det_days, cha_days = self._compute_charges(
            terms, gated_out_day, returned_day
        )
        self._detention_days = det_days

        if det_days == 0:
            self.risk = RiskLevel.GREEN
        else:
            self.risk = RiskLevel.RED

        return DetentionResult(
            container_id=container.container_id,
            evidence=self.evidence,
            detention_days=det_days,
            chassis_days=cha_days,
            accrued_usd=accrued,
            prevented_usd=self.prevented_usd,
            return_slot=self.return_slot,
            empty_returned_at=self.empty_returned_at,
            near_miss=self.near_miss,
        )

    # ---------------- internals ----------------

    async def _decide_return(
        self,
        container: ContainerInput,
        terms: ContractTerms,
        gated_out_day: date,
        detention_due: date,
        inp: DetentionInput,
    ) -> None:
        """Price the options and book the return. The $1,485 decision."""
        # Chassis accrues on the parked empty, so its clock starts after
        # stripping rather than at gate-out.
        chassis_start = (
            self.cargo_stripped_at.date()
            if self.cargo_stripped_at
            else gated_out_day + timedelta(days=3)
        )
        projected_return = detention_due + timedelta(days=6)  # the unmanaged path

        options = await workflow.execute_activity(
            act.price_detention_options,
            act.PriceDetentionInput(
                container_id=container.container_id,
                as_of=_now(),
                terms=terms,
                gated_out_at=gated_out_day,
                projected_return=projected_return,
                chassis_start=chassis_start,
                depot_restriction_prior=inp.depot_restriction_prior,
                auto_approve_limit_usd=inp.auto_approve_limit_usd,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

        choice = options.best()
        if choice.action == "do_nothing":
            return

        if choice.requires_approval or choice.cost_usd > inp.auto_approve_limit_usd:
            await workflow.execute_activity(
                act.notify_human,
                act.NotifyHumanInput(
                    container_id=container.container_id,
                    reason="detention prevention above spend cap",
                    action=choice.action,
                    cost_usd=choice.cost_usd,
                    detail=choice.note,
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )
            await workflow.wait_condition(lambda: choice.action in self.approvals)

        # Book two days inside free time. Slack is free here, so buy it.
        slot = datetime.combine(
            detention_due - timedelta(days=2), datetime.min.time()
        ) + timedelta(hours=9)

        booking = await workflow.execute_activity(
            act.book_container_empty_return,
            act.BookEmptyReturnInput(
                container_id=container.container_id,
                depot=container.return_depot,
                slot_at=slot,
            ),
            start_to_close_timeout=LONG,
            retry_policy=IO_RETRY,
        )

        self.return_booking = booking
        self.return_slot = booking.slot_at
        self.spend_usd += choice.cost_usd
        self.prevented_usd = choice.gross_saving_usd

        self.evidence.append(
            Evidence(
                kind=EventKind.APPOINTMENT_BOOKED,
                occurred_at=_now(),
                source_system="depot",
                source_document_id=booking.booking_id,
                summary=f"empty return pre-booked for {slot.date().isoformat()}",
            )
        )

    async def _watch_until_returned(
        self, container: ContainerInput, inp: DetentionInput, detention_due: date
    ) -> None:
        """Confirm the slot stays valid and classify advisories as they arrive."""
        # Bounded so the arc cannot watch forever, but measured from whichever
        # is later: the due date, or now. A domain deadline can already be in
        # the past when the timeline is historical or the workflow started late,
        # and abandoning the watch on entry would drop the return we are here to
        # observe.
        watch_days = timedelta(days=14)
        deadline = max(
            datetime.combine(detention_due, datetime.min.time()) + watch_days,
            _now() + watch_days,
        )

        while self.empty_returned_at is None and _now() < deadline:
            if self._pending_advisory is not None:
                await self._handle_advisory(container, inp)

            try:
                await workflow.wait_condition(
                    lambda: self.empty_returned_at is not None
                    or self._pending_advisory is not None,
                    timeout=RETURN_WATCH_INTERVAL,
                )
            except asyncio.TimeoutError:
                continue

    async def _handle_advisory(self, container: ContainerInput, inp: DetentionInput) -> None:
        """LLM. Match the advisory to this depot, then decide whether it matters."""
        text, doc_id, at = self._pending_advisory  # type: ignore[misc]
        self._pending_advisory = None

        result = await workflow.execute_activity(
            act.classify_carrier_advisory,
            act.ClassifyAdvisoryInput(
                advisory_text=text,
                depot=container.return_depot,
                carrier=container.carrier,
            ),
            start_to_close_timeout=LONG,
            retry_policy=LLM_RETRY,
        )
        self.restriction = result

        self.evidence.append(
            Evidence(
                kind=EventKind.CARRIER_ADVISORY,
                occurred_at=at,
                source_system="carrier_advisory",
                source_document_id=doc_id,
                summary=(
                    f"advisory {'matched' if result.affects_this_container else 'not applicable'} "
                    f"for {container.return_depot}"
                ),
            )
        )

        if not result.affects_this_container:
            return

        # Already back. The restriction validates the choice rather than
        # triggering an action - a labelled training signal for the learning
        # store, which raises the return-urgency prior for this depot.
        already_back = self.empty_returned_at is not None
        booked_before = (
            self.return_slot is not None
            and result.restriction_starts is not None
            and self.return_slot.date() < result.restriction_starts
        )

        if already_back or booked_before:
            self.near_miss = True
            self.evidence.append(
                Evidence(
                    kind=EventKind.NEAR_MISS,
                    occurred_at=_now(),
                    source_system="learning_store",
                    source_document_id=f"near-miss::{doc_id}",
                    summary=(
                        "restriction would have caused detention; empty already "
                        "booked or returned ahead of it"
                    ),
                )
            )
            return

        # Not back yet and the restriction bites. Escalate - there is no
        # autonomous action that fixes a depot refusing the box.
        await workflow.execute_activity(
            act.notify_human,
            act.NotifyHumanInput(
                container_id=container.container_id,
                reason="empty return restriction active and box not yet returned",
                action="escalate_return_restriction",
                detail=result.reasoning,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

    def _compute_charges(
        self, terms: ContractTerms, gated_out_day: date, returned_day: date
    ) -> tuple[Decimal, int, int]:
        from agents.shared.charges import charge_for_period

        det_total, det_break = charge_for_period(
            terms.clock(ChargeType.DETENTION), gated_out_day, returned_day
        )

        cha_total = Decimal("0")
        cha_break: list = []
        if ChargeType.CHASSIS in terms.clocks:
            chassis_start = (
                self.cargo_stripped_at.date() if self.cargo_stripped_at else gated_out_day
            )
            cha_total, cha_break = charge_for_period(
                terms.clock(ChargeType.CHASSIS), chassis_start, returned_day
            )

        return det_total + cha_total, len(det_break), len(cha_break)

    async def _sleep_for(self, delta: timedelta) -> None:
        try:
            await workflow.wait_condition(
                lambda: self.empty_returned_at is not None or self.reassess_now,
                timeout=delta,
            )
            self.reassess_now = False
        except asyncio.TimeoutError:
            pass
