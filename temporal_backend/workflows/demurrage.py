"""DemurrageArc: the terminal clock. Days 8-20.

Owns availability polling, effective-LFD calculation, the checkpoint loop and
interventions. Ends on gate-out.

Nothing in this file may be non-deterministic: no LLM calls, no ``random``, no
wall-clock reads outside ``workflow.now()``, no I/O. All of that is in activities.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from agents.shared.models import (
        AppointmentAttempt,
        ChargeType,
        ContainerInput,
        ContractTerms,
        Evidence,
        EventKind,
        Hold,
        HoldType,
        LfdCalculation,
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


# Retry policy for flaky terminal portals and carrier APIs.
IO_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=6,
)

# LLM activities get fewer attempts: a failed free-tier request still burns quota.
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
)

SHORT = timedelta(seconds=30)
LONG = timedelta(minutes=5)

AVAILABILITY_POLL_INTERVAL = timedelta(hours=4)
SLOT_SCAN_INTERVAL = timedelta(minutes=20)
DAILY = timedelta(hours=24)
CHECKPOINT_OFFSETS_HOURS = (72, 48, 24)

# Read once at worker start, not per-workflow-call: consistent across every
# replay within this worker process, which is what determinism requires.
#
# Checkpoint targets are anchored to effective_lfd at midnight - a fixed
# calendar instant - so merely shrinking CHECKPOINT_OFFSETS_HOURS does not make
# a checkpoint fire soon; the target is still wherever that midnight falls,
# which could be seconds or up to ~24h from "now" depending on time of day.
# Demo mode sidesteps the date anchoring entirely: checkpoints become short
# delays measured from when the checkpoint loop starts, not from a calendar
# date, so the first one fires in seconds regardless of wall-clock time. It
# changes nothing about pricing, suppression, or the approval gate itself -
# only when the loop chooses to look.
DEMO_MODE = os.getenv("PF_DEMO_MODE", "") == "1"
DEMO_CHECKPOINT_DELAYS = (timedelta(seconds=5), timedelta(seconds=15), timedelta(seconds=30))


class DemurrageResult(BaseModel):
    """Handed back to the parent when the arc closes."""

    container_id: str
    evidence: list[Evidence] = Field(default_factory=list)
    lfd: LfdCalculation | None = None
    billed_days: int = 0
    accrued_usd: Decimal = Decimal("0")
    appointment_failures: int = 0
    spend_usd: Decimal = Decimal("0")
    gated_out_at: datetime | None = None


class DemurrageInput(BaseModel):
    container: ContainerInput
    terms: ContractTerms
    discharged_at: datetime
    auto_approve_limit_usd: Decimal = Decimal("250")
    slot_scarcity_prior: float = 0.0


@workflow.defn
class DemurrageArc:
    """The terminal clock, from discharge to gate-out."""

    def __init__(self) -> None:
        self.evidence: list[Evidence] = []
        self.holds: list[Hold] = []
        self.available_at: datetime | None = None
        self.gated_out_at: datetime | None = None
        self.gate_out_doc_id: str = ""
        self.lfd: LfdCalculation | None = None
        # The availability timestamp self.lfd was derived from, so a later
        # report can be detected and the calculation redone.
        self.lfd_basis: datetime | None = None
        self.appointment_failures = 0
        self.spend_usd = Decimal("0")
        self.approvals: set[str] = set()
        self.reassess_now = False
        self.intervention_suppressed = False
        self.risk = RiskLevel.PENDING
        self.risk_reason = ""
        self.counterfactual_usd = Decimal("0")

    # ---------------- signals ----------------

    @workflow.signal
    def container_available(self, at: datetime, source_document_id: str) -> None:
        if self.available_at is None:
            self.available_at = at
            self.evidence.append(
                Evidence(
                    kind=EventKind.CONTAINER_AVAILABLE,
                    occurred_at=at,
                    source_system="terminal_portal",
                    source_document_id=source_document_id,
                    summary="container grounded and appointable",
                )
            )
        self.reassess_now = True

    @workflow.signal
    def hold_placed(self, hold: Hold) -> None:
        """Wakes the sleeping workflow. Suppression is decided by the assessment."""
        self.holds.append(hold)
        self.evidence.append(
            Evidence(
                kind=EventKind.HOLD_PLACED,
                occurred_at=hold.placed_at,
                source_system="customs",
                source_document_id=hold.reference or f"hold::{hold.hold_type.value}",
                summary=f"{hold.hold_type.value} hold placed",
            )
        )
        self.reassess_now = True

    @workflow.signal
    def hold_released(self, hold_type: HoldType, at: datetime, source_document_id: str) -> None:
        """Immediate reassessment rather than waiting for the next checkpoint.

        At $575/day the difference between reacting in seconds and reacting
        tomorrow morning is worth having.
        """
        for hold in self.holds:
            if hold.hold_type == hold_type and hold.is_open:
                hold.released_at = at
        self.evidence.append(
            Evidence(
                kind=EventKind.HOLD_RELEASED,
                occurred_at=at,
                source_system="customs",
                source_document_id=source_document_id,
                summary=f"{hold_type.value} hold released",
            )
        )
        self.reassess_now = True

    @workflow.signal
    def gate_out(self, at: datetime, source_document_id: str) -> None:
        self.gated_out_at = at
        self.gate_out_doc_id = source_document_id
        self.evidence.append(
            Evidence(
                kind=EventKind.GATE_OUT,
                occurred_at=at,
                source_system="terminal_portal",
                source_document_id=source_document_id,
                summary="container gated out of the terminal",
            )
        )
        self.reassess_now = True

    @workflow.signal
    def approve(self, action: str) -> None:
        self.approvals.add(action)

    # ---------------- queries ----------------

    @workflow.query
    def state(self) -> dict:
        """The control tower's read path. The workflow *is* the read model."""
        return {
            "risk": self.risk.value,
            "reason": self.risk_reason,
            "effective_lfd": self.lfd.effective_lfd.isoformat() if self.lfd else None,
            "nominal_lfd": self.lfd.nominal_lfd.isoformat() if self.lfd else None,
            "lfd_shifted": self.lfd.shifted if self.lfd else False,
            "holds": sorted(h.hold_type.value for h in self.holds if h.is_open),
            "intervention_suppressed": self.intervention_suppressed,
            "spend_usd": str(self.spend_usd),
            "appointment_failures": self.appointment_failures,
            "evidence_count": len(self.evidence),
            "counterfactual_usd": str(self.counterfactual_usd),
            "gated_out_at": self.gated_out_at.isoformat() if self.gated_out_at else None,
        }

    # ---------------- run ----------------

    @workflow.run
    async def run(self, inp: DemurrageInput) -> DemurrageResult:
        container = inp.container
        terms = inp.terms
        demurrage = terms.clock(ChargeType.DEMURRAGE)
        clock_start = inp.discharged_at.date()

        # Phase 1: poll for availability. Every miss is recorded as evidence,
        # never as a retryable error.
        await self._poll_availability(container, inp.discharged_at)

        await self._compute_lfd(terms, inp.discharged_at)

        # Phase 2: checkpoints at LFD-72h, -48h, -24h, then daily.
        await self._checkpoint_loop(container, terms, clock_start, inp)

        # Phase 3: the box is released and action is finally possible. Scan.
        if self.gated_out_at is None:
            await self._scan_for_slots(container, terms, clock_start, inp)

        # Wait for gate-out if it has not already arrived.
        await workflow.wait_condition(lambda: self.gated_out_at is not None)

        # Settle the calculation on everything now known. Availability is often
        # reported after the polling window closed, and the figure handed to the
        # parent is the one the dispute is later built on.
        if self.available_at != self.lfd_basis:
            await self._compute_lfd(terms, inp.discharged_at)

        billed_end = self.gated_out_at.date()  # type: ignore[union-attr]
        accrued = await self._accrued(terms, clock_start, billed_end)

        return DemurrageResult(
            container_id=container.container_id,
            evidence=self.evidence,
            lfd=self.lfd,
            billed_days=self._billed_day_count(demurrage, clock_start, billed_end),
            accrued_usd=accrued,
            appointment_failures=self.appointment_failures,
            spend_usd=self.spend_usd,
            gated_out_at=self.gated_out_at,
        )

    # ---------------- internals ----------------

    async def _compute_lfd(self, terms: ContractTerms, discharged_at: datetime) -> None:
        """(Re)compute the effective last free day from what is known right now.

        Not a one-shot calculation. Availability is routinely reported late -
        that lateness is the whole of Leak 01 - and it arrives by signal, after
        the initial polling window has closed. Recomputing when the timestamp
        finally lands is what turns a late report into a shifted last free day
        rather than an argument we never made.
        """
        self.lfd = await workflow.execute_activity(
            act.compute_effective_lfd,
            act.EffectiveLfdInput(
                terms=terms,
                discharged_at=discharged_at,
                available_at=self.available_at,
                availability_misses=[
                    e
                    for e in self.evidence
                    if e.kind in (EventKind.AVAILABILITY_MISS, EventKind.CONTAINER_AVAILABLE)
                ],
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )
        self.lfd_basis = self.available_at

    async def _poll_availability(self, container: ContainerInput, discharged_at: datetime) -> None:
        """Poll until grounded. Each miss is a successful adverse observation."""
        deadline = discharged_at + timedelta(days=5)

        while self.available_at is None and self.gated_out_at is None:
            if _now() >= deadline:
                break

            attempt: AppointmentAttempt = await workflow.execute_activity(
                act.reserve_terminal_appointment,
                act.ReserveAppointmentInput(
                    container_id=container.container_id,
                    terminal=container.terminal,
                    attempted_at=_now(),
                    slot_available=False,  # availability probe, not a booking
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )

            self.evidence.append(
                Evidence(
                    kind=EventKind.AVAILABILITY_MISS,
                    occurred_at=attempt.attempted_at,
                    source_system="terminal_portal",
                    source_document_id=attempt.reference,
                    summary="container not yet grounded or appointable",
                )
            )

            try:
                await workflow.wait_condition(
                    lambda: self.available_at is not None or self.gated_out_at is not None,
                    timeout=AVAILABILITY_POLL_INTERVAL,
                )
            except asyncio.TimeoutError:
                continue

    async def _checkpoint_loop(
        self,
        container: ContainerInput,
        terms: ContractTerms,
        clock_start: date,
        inp: DemurrageInput,
    ) -> None:
        """Price options while action is still cheap, then reassess daily."""
        assert self.lfd is not None
        effective_lfd = self.lfd.effective_lfd

        if DEMO_MODE:
            for delay in DEMO_CHECKPOINT_DELAYS:
                if self.gated_out_at is not None:
                    return
                await self._sleep_until(_now() + delay)
                await self._assess_and_maybe_act(container, terms, clock_start, inp)
        else:
            for hours in CHECKPOINT_OFFSETS_HOURS:
                if self.gated_out_at is not None:
                    return
                target = datetime.combine(effective_lfd, datetime.min.time()) - timedelta(hours=hours)
                if target <= _now():
                    continue
                await self._sleep_until(target)
                await self._assess_and_maybe_act(container, terms, clock_start, inp)

        # Past the LFD: reassess daily while blocked.
        while self.gated_out_at is None:
            if self._open_holds():
                await self._assess_and_maybe_act(container, terms, clock_start, inp)
                try:
                    await workflow.wait_condition(
                        lambda: self.reassess_now or self.gated_out_at is not None,
                        timeout=DAILY,
                    )
                    self.reassess_now = False
                except asyncio.TimeoutError:
                    pass
            else:
                return

    async def _scan_for_slots(
        self,
        container: ContainerInput,
        terms: ContractTerms,
        clock_start: date,
        inp: DemurrageInput,
    ) -> None:
        """Continuous slot scanning. The evidence machine.

        Rate limiting is enforced at the worker level, not here: one task queue
        with a concurrency cap gives fleet-wide politeness toward the terminal API
        with no coordination between the workflows doing the scanning.
        """
        scans = 0
        max_scans = 220  # bounded so history stays sane in the demo

        while self.gated_out_at is None and scans < max_scans:
            scans += 1

            attempt: AppointmentAttempt = await workflow.execute_activity(
                act.reserve_terminal_appointment,
                act.ReserveAppointmentInput(
                    container_id=container.container_id,
                    terminal=container.terminal,
                    attempted_at=_now(),
                    slot_available=False,
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )

            if attempt.succeeded:
                self.evidence.append(
                    Evidence(
                        kind=EventKind.APPOINTMENT_BOOKED,
                        occurred_at=attempt.attempted_at,
                        source_system="terminal_portal",
                        source_document_id=attempt.reference,
                        summary="appointment secured",
                    )
                )
                return

            self.appointment_failures += 1
            self.evidence.append(
                Evidence(
                    kind=EventKind.APPOINTMENT_UNAVAILABLE,
                    occurred_at=attempt.attempted_at,
                    source_system="terminal_portal",
                    source_document_id=attempt.reference,
                    summary=f"no appointment slots offered by {container.terminal}",
                )
            )

            try:
                await workflow.wait_condition(
                    lambda: self.gated_out_at is not None,
                    timeout=SLOT_SCAN_INTERVAL,
                )
            except asyncio.TimeoutError:
                continue

    async def _assess_and_maybe_act(
        self,
        container: ContainerInput,
        terms: ContractTerms,
        clock_start: date,
        inp: DemurrageInput,
    ) -> None:
        assert self.lfd is not None

        # Availability may have been reported since the last calculation.
        if self.available_at != self.lfd_basis:
            await self._compute_lfd(terms, inp.discharged_at)

        assessment = await workflow.execute_activity(
            act.assess_container_risk,
            act.AssessRiskInput(
                container_id=container.container_id,
                terms=terms,
                as_of=_now(),
                effective_lfd=self.lfd.effective_lfd,
                clock_start=clock_start,
                holds=self.holds,
                projected_end=self.lfd.effective_lfd + timedelta(days=6),
                gated_out=self.gated_out_at is not None,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

        self.risk = assessment.level
        self.risk_reason = assessment.reason
        self.counterfactual_usd = assessment.counterfactual_usd
        self.intervention_suppressed = assessment.intervention_suppressed

        options = await workflow.execute_activity(
            act.price_demurrage_options,
            act.PriceDemurrageInput(
                container_id=container.container_id,
                as_of=_now(),
                exposure_usd=assessment.counterfactual_usd,
                intervention_suppressed=assessment.intervention_suppressed,
                suppression_reason=assessment.suppression_reason,
                slot_scarcity_prior=inp.slot_scarcity_prior,
                auto_approve_limit_usd=inp.auto_approve_limit_usd,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

        choice = options.best()

        # Suppressed, or nothing worth doing. Do nothing and keep recording -
        # and the counterfactual ledger later proves that was correct.
        if self.intervention_suppressed or choice.action == "do_nothing":
            return
        if choice.expected_saving_usd <= 0:
            return

        # Spend cap enforced in workflow code, where it can be audited, rather
        # than in a prompt, where it can be talked around.
        if choice.requires_approval or choice.cost_usd > inp.auto_approve_limit_usd:
            await workflow.execute_activity(
                act.notify_human,
                act.NotifyHumanInput(
                    container_id=container.container_id,
                    reason=assessment.reason,
                    action=choice.action,
                    cost_usd=choice.cost_usd,
                    detail=choice.note,
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )
            # Parks indefinitely. No polling loop, no expiring job.
            await workflow.wait_condition(lambda: choice.action in self.approvals)

        await self._act_with_compensation(container, choice.action, choice.cost_usd)

    async def _act_with_compensation(
        self, container: ContainerInput, action: str, cost_usd: Decimal
    ) -> None:
        """Saga. Every side effect registers its compensator before the next step.

        This is what stops an autonomous agent leaving phantom bookings and
        no-show fees behind in real carrier systems.
        """
        compensations: list[tuple] = []

        try:
            booking = await workflow.execute_activity(
                act.book_container_drayage,
                act.BookDrayageInput(
                    container_id=container.container_id,
                    provider="premium_drayage" if "expedite" in action else "standard_drayage",
                    slot_at=_now() + timedelta(hours=12),
                    cost_usd=cost_usd,
                    expedited="expedite" in action,
                ),
                start_to_close_timeout=LONG,
                retry_policy=IO_RETRY,
            )
            compensations.append((act.cancel_container_drayage, booking))
            self.spend_usd += cost_usd

            attempt = await workflow.execute_activity(
                act.reserve_terminal_appointment,
                act.ReserveAppointmentInput(
                    container_id=container.container_id,
                    terminal=container.terminal,
                    attempted_at=_now(),
                    slot_available=False,
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )

            if not attempt.succeeded:
                # Leak 03, captured as evidence *before* the unwind.
                self.appointment_failures += 1
                self.evidence.append(
                    Evidence(
                        kind=EventKind.APPOINTMENT_UNAVAILABLE,
                        occurred_at=attempt.attempted_at,
                        source_system="terminal_portal",
                        source_document_id=attempt.reference,
                        summary="no slot available; drayage booking unwound",
                    )
                )
                raise ApplicationFailureNoSlot()

        except ApplicationFailureNoSlot:
            for fn, arg in reversed(compensations):
                await workflow.execute_activity(
                    fn, arg, start_to_close_timeout=LONG, retry_policy=IO_RETRY
                )
            self.spend_usd -= cost_usd  # the booking was undone, so was the spend

    def _open_holds(self) -> list[Hold]:
        return [h for h in self.holds if h.is_open]

    async def _sleep_until(self, target: datetime) -> None:
        """Durable sleep raced against any incoming signal."""
        delta = target - _now()
        if delta.total_seconds() <= 0:
            return
        try:
            await workflow.wait_condition(
                lambda: self.reassess_now or self.gated_out_at is not None,
                timeout=delta,
            )
            self.reassess_now = False
        except asyncio.TimeoutError:
            pass

    async def _accrued(self, terms: ContractTerms, start: date, end: date) -> Decimal:
        from agents.shared.charges import charge_for_period

        total, _ = charge_for_period(terms.clock(ChargeType.DEMURRAGE), start, end)
        return total

    def _billed_day_count(self, clock, start: date, end: date) -> int:
        from agents.shared.charges import charge_for_period

        _, breakdown = charge_for_period(clock, start, end)
        return len(breakdown)


class ApplicationFailureNoSlot(Exception):
    """Internal saga trigger. Not a retryable failure."""
