"""ContainerWorkflow: the parent entity workflow. Day 1 to 45+.

One workflow instance per real-world object, alive for the container's whole
life. Started with a deterministic ID of ``container::MSKU7481920``, which makes
the system idempotent - a duplicate arrival notice cannot create a second agent.

Children report; the parent remembers. Each arc closes when its clock stops, but
the dispute needs facts from all three arcs filed weeks later, so evidence
accumulates here and is passed down to ``DisputeArc`` at spawn time.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from agents.shared.models import (
        ChargeType,
        ContainerInput,
        ContractTerms,
        Evidence,
        EventKind,
        Hold,
        HoldType,
        Invoice,
        LfdCalculation,
        RiskLevel,
    )
    from temporal_backend.activities import registry as act
    from temporal_backend.workflows.demurrage import (
        DemurrageArc,
        DemurrageInput,
        DemurrageResult,
    )
    from temporal_backend.workflows.detention import (
        DetentionArc,
        DetentionInput,
        DetentionResult,
    )
    from temporal_backend.workflows.dispute import DisputeArc, DisputeInput

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

# How long to keep the parent alive after the box is gone, waiting for invoices.
INVOICE_WATCH_DAYS = 60


class ContainerWorkflowInput(BaseModel):
    container: ContainerInput
    contract_text: str = ""
    contract_pdf_path: str | None = None
    auto_approve_limit_usd: Decimal = Decimal("250")
    slot_scarcity_prior: float = 0.0
    depot_restriction_prior: float = 0.0


class ContainerResult(BaseModel):
    container_id: str
    total_billed_usd: Decimal = Decimal("0")
    total_prevented_usd: Decimal = Decimal("0")
    total_contested_usd: Decimal = Decimal("0")
    demurrage_days: int = 0
    detention_days: int = 0
    evidence_count: int = 0
    disputes_filed: list[str] = Field(default_factory=list)
    terms_confidence: float = 0.0


@workflow.defn
class ContainerWorkflow:
    """Owns identity, contract terms, the event log and all evidence."""

    def __init__(self) -> None:
        self.terms: ContractTerms | None = None
        self.evidence: list[Evidence] = []
        self.discharged_at: datetime | None = None
        self.gated_out_at: datetime | None = None
        self.empty_returned_at: datetime | None = None
        self.pending_invoices: list[Invoice] = []
        self.processed_invoices: list[str] = []
        self.dispute_ids: list[str] = []
        self.holds: list[Hold] = []

        self.demurrage: DemurrageResult | None = None
        self.detention: DetentionResult | None = None
        self.lfd: LfdCalculation | None = None

        self.total_billed = Decimal("0")
        self.total_prevented = Decimal("0")
        self.total_contested = Decimal("0")
        self.closed = False
        self.risk = RiskLevel.PENDING
        self.protest_held = False

    # ---------------- signals ----------------

    @workflow.signal
    def customs_entry_filed(self, at: datetime, source_document_id: str) -> None:
        """Recorded as a pre-emptive defence.

        If a hold lands later, this fact rules out one carrier defence: that the
        delay was caused by late filing.
        """
        self.evidence.append(
            Evidence(
                kind=EventKind.CUSTOMS_ENTRY_FILED,
                occurred_at=at,
                source_system="ACE",
                source_document_id=source_document_id,
                summary="customs entry filed in advance of arrival",
            )
        )

    @workflow.signal
    def discharged(self, at: datetime, source_document_id: str) -> None:
        """Releases the pre-arrival wait and starts the demurrage arc."""
        if self.discharged_at is None:
            self.discharged_at = at
            self.evidence.append(
                Evidence(
                    kind=EventKind.DISCHARGED,
                    occurred_at=at,
                    source_system="EDI_315",
                    source_document_id=source_document_id,
                    summary="container discharged from vessel",
                )
            )

    @workflow.signal
    def invoice_received(self, invoice: Invoice) -> None:
        """Spawns one DisputeArc per invoice and starts the regulatory timer."""
        if invoice.invoice_id in self.processed_invoices:
            return  # idempotent: a duplicate invoice cannot double-file
        self.pending_invoices.append(invoice)
        self.evidence.append(
            Evidence(
                kind=EventKind.INVOICE_RECEIVED,
                occurred_at=datetime.combine(invoice.issued_at, datetime.min.time()),
                source_system="ap_inbox",
                source_document_id=invoice.source_document_id
                or f"invoice::{invoice.invoice_id}",
                summary=f"{invoice.charge_type.value} invoice ${invoice.total_usd} received",
            )
        )

    @workflow.signal
    def paid_under_protest(self, invoice_id: str, at: datetime) -> None:
        """Leak 07. The protest is held as workflow state so it carries forward.

        Cargo release is often withheld pending settlement; the importer pays to
        free the goods intending to dispute afterwards. Nothing normally captures
        that intent, and the protest quietly becomes acceptance.
        """
        self.protest_held = True
        self.evidence.append(
            Evidence(
                kind=EventKind.INVOICE_RECEIVED,
                occurred_at=at,
                source_system="finance",
                source_document_id=f"protest::{invoice_id}",
                summary="paid under protest; recovery still being prosecuted",
            )
        )

    @workflow.signal
    def close(self) -> None:
        self.closed = True

    # ---------------- queries ----------------

    @workflow.query
    def state(self) -> dict:
        """Live state and audit trail are the same object.

        That equivalence is what makes the dispute defensible.
        """
        return {
            "risk": self.risk.value,
            "terms_loaded": self.terms is not None,
            "terms_confidence": self.terms.confidence if self.terms else 0.0,
            "discharged_at": self.discharged_at.isoformat() if self.discharged_at else None,
            "gated_out_at": self.gated_out_at.isoformat() if self.gated_out_at else None,
            "empty_returned_at": (
                self.empty_returned_at.isoformat() if self.empty_returned_at else None
            ),
            "effective_lfd": self.lfd.effective_lfd.isoformat() if self.lfd else None,
            "nominal_lfd": self.lfd.nominal_lfd.isoformat() if self.lfd else None,
            "evidence_count": len(self.evidence),
            "demurrage_days": self.demurrage.billed_days if self.demurrage else 0,
            "detention_days": self.detention.detention_days if self.detention else 0,
            "spend_usd": str(self.demurrage.spend_usd if self.demurrage else Decimal("0")),
            "total_billed_usd": str(self.total_billed),
            "total_prevented_usd": str(self.total_prevented),
            "total_contested_usd": str(self.total_contested),
            "disputes": list(self.dispute_ids),
            "protest_held": self.protest_held,
        }

    @workflow.query
    def evidence_log(self) -> list[dict]:
        return [
            {
                "kind": e.kind.value,
                "occurred_at": e.occurred_at.isoformat(),
                "source_document_id": e.source_document_id,
                "summary": e.summary,
            }
            for e in sorted(self.evidence, key=lambda x: x.occurred_at)
        ]

    # ---------------- run ----------------

    @workflow.run
    async def run(self, inp: ContainerWorkflowInput) -> ContainerResult:
        container = inp.container

        # Day 1. Load the terms while it is cheap. This is the single
        # highest-leverage activity in the entire 45 days.
        self.terms = await workflow.execute_activity(
            act.load_contract_terms,
            act.LoadTermsInput(
                contract_id=container.contract_id,
                port=container.port,
                carrier=container.carrier,
                contract_text=inp.contract_text,
                contract_pdf_path=inp.contract_pdf_path,
                source_document_id=f"contract::{container.contract_id}",
            ),
            start_to_close_timeout=LONG,
            retry_policy=LLM_RETRY,
        )

        # Low-confidence extraction goes to a human now, on day 1, when there is
        # no time pressure at all.
        if self.terms.confidence < 0.70:
            await workflow.execute_activity(
                act.notify_human,
                act.NotifyHumanInput(
                    container_id=container.container_id,
                    reason=f"contract term extraction confidence {self.terms.confidence:.2f}",
                    action="review_contract_terms",
                    detail="; ".join(self.terms.extraction_notes),
                ),
                start_to_close_timeout=SHORT,
                retry_policy=IO_RETRY,
            )

        # Days 1-7. Exist, but do nothing expensive. Sleeping holds full state
        # and consumes no worker capacity - which is why one agent per container
        # is affordable at all.
        await workflow.wait_condition(lambda: self.discharged_at is not None)

        # Days 8-20. The terminal clock.
        self.demurrage = await workflow.execute_child_workflow(
            DemurrageArc.run,
            DemurrageInput(
                container=container,
                terms=self.terms,
                discharged_at=self.discharged_at,  # type: ignore[arg-type]
                auto_approve_limit_usd=inp.auto_approve_limit_usd,
                slot_scarcity_prior=inp.slot_scarcity_prior,
            ),
            id=f"demurrage::{container.container_id}",
            task_queue=workflow.info().task_queue,
        )

        self.evidence.extend(self.demurrage.evidence)
        self.lfd = self.demurrage.lfd
        self.gated_out_at = self.demurrage.gated_out_at
        self.risk = RiskLevel.RED if self.demurrage.billed_days else RiskLevel.GREEN

        # Days 20-31. The equipment clock. This is where a third of the money
        # lives, and where the human process has already stopped watching.
        if self.gated_out_at is not None:
            self.detention = await workflow.execute_child_workflow(
                DetentionArc.run,
                DetentionInput(
                    container=container,
                    terms=self.terms,
                    gated_out_at=self.gated_out_at,
                    auto_approve_limit_usd=inp.auto_approve_limit_usd,
                    depot_restriction_prior=inp.depot_restriction_prior,
                ),
                id=f"detention::{container.container_id}",
                task_queue=workflow.info().task_queue,
            )
            self.evidence.extend(self.detention.evidence)
            self.empty_returned_at = self.detention.empty_returned_at
            self.total_prevented += self.detention.prevented_usd

        # Days 40+. One dispute per invoice, each outliving this workflow.
        await self._handle_invoices(container)

        return ContainerResult(
            container_id=container.container_id,
            total_billed_usd=self.total_billed,
            total_prevented_usd=self.total_prevented,
            total_contested_usd=self.total_contested,
            demurrage_days=self.demurrage.billed_days if self.demurrage else 0,
            detention_days=self.detention.detention_days if self.detention else 0,
            evidence_count=len(self.evidence),
            disputes_filed=list(self.dispute_ids),
            terms_confidence=self.terms.confidence,
        )

    # ---------------- internals ----------------

    async def _handle_invoices(self, container: ContainerInput) -> None:
        """Spawn a DisputeArc per invoice, abandoning each so it outlives us.

        The invoice watch is not only looking for arrival. An invoice issued more
        than 30 days after the last incurred charge is itself unenforceable, so a
        timer that notices *nothing arrived* is as valuable as one that notices
        something did.
        """
        deadline = _now() + timedelta(days=INVOICE_WATCH_DAYS)

        while not self.closed and _now() < deadline:
            try:
                await workflow.wait_condition(
                    lambda: bool(self.pending_invoices) or self.closed,
                    timeout=timedelta(days=1),
                )
            except asyncio.TimeoutError:
                continue

            while self.pending_invoices:
                invoice = self.pending_invoices.pop(0)
                self.processed_invoices.append(invoice.invoice_id)
                self.total_billed += invoice.total_usd
                await self._spawn_dispute(container, invoice)

    async def _spawn_dispute(self, container: ContainerInput, invoice: Invoice) -> None:
        assert self.terms is not None

        clock_start, clock_end = self._clock_window(invoice)
        exam, capacity, ungrounded = self._claim_amounts(invoice)

        dispute_id = f"dispute::{invoice.invoice_id}"

        # ABANDON is what lets the dispute run for months independently of
        # everything else, long after this workflow has finished.
        await workflow.start_child_workflow(
            DisputeArc.run,
            DisputeInput(
                container_id=container.container_id,
                invoice=invoice,
                terms=self.terms,
                evidence=self.evidence,
                lfd=self.lfd,
                clock_start=clock_start,
                clock_end=clock_end,
                exam_days_usd=exam,
                capacity_days_usd=capacity,
                ungrounded_days_usd=ungrounded,
            ),
            id=dispute_id,
            task_queue=workflow.info().task_queue,
            parent_close_policy=ParentClosePolicy.ABANDON,
        )

        self.dispute_ids.append(dispute_id)
        self.total_contested += invoice.total_usd

    def _clock_window(self, invoice: Invoice) -> tuple[date | None, date | None]:
        """Which clock this invoice bills against."""
        if invoice.charge_type in (ChargeType.DEMURRAGE, ChargeType.STORAGE):
            start = self.discharged_at.date() if self.discharged_at else None
            end = self.gated_out_at.date() if self.gated_out_at else None
            return start, end

        start = self.gated_out_at.date() if self.gated_out_at else None
        end = self.empty_returned_at.date() if self.empty_returned_at else None
        return start, end

    def _claim_amounts(self, invoice: Invoice) -> tuple[Decimal, Decimal, Decimal]:
        """Attribute billed dollars to the leaks that explain them.

        Only meaningful for the terminal-side clocks; detention that never
        accrued needs no attribution because the EIR settles it outright.
        """
        if invoice.charge_type not in (ChargeType.DEMURRAGE, ChargeType.STORAGE):
            return Decimal("0"), Decimal("0"), Decimal("0")

        exam_days = sum(
            1
            for e in self.evidence
            if e.kind == EventKind.HOLD_PLACED
        )
        capacity_days = len(
            {
                e.occurred_at.date()
                for e in self.evidence
                if e.kind == EventKind.APPOINTMENT_UNAVAILABLE
            }
        )
        ungrounded = self.lfd.ungrounded_days if self.lfd else 0

        lines = invoice.lines
        if not lines:
            # No line detail: split the total by day counts we can evidence.
            return Decimal("0"), Decimal("0"), Decimal("0")

        by_day = {ln.charge_date: ln.amount_usd for ln in lines}
        capacity_dates = {
            e.occurred_at.date()
            for e in self.evidence
            if e.kind == EventKind.APPOINTMENT_UNAVAILABLE
        }
        hold_dates = self._hold_date_range()

        capacity_usd = sum(
            (amt for day, amt in by_day.items() if day in capacity_dates), Decimal("0")
        )
        exam_usd = sum(
            (amt for day, amt in by_day.items() if day in hold_dates), Decimal("0")
        )
        ungrounded_usd = Decimal("0")
        if self.lfd and self.lfd.shifted:
            shifted_days = sorted(
                day
                for day in by_day
                if self.lfd.nominal_lfd < day <= self.lfd.effective_lfd
            )
            ungrounded_usd = sum((by_day[d] for d in shifted_days), Decimal("0"))

        return exam_usd, capacity_usd, ungrounded_usd

    def _hold_date_range(self) -> set[date]:
        """Every date covered by an open-then-closed hold window."""
        placed = [
            e.occurred_at.date() for e in self.evidence if e.kind == EventKind.HOLD_PLACED
        ]
        released = [
            e.occurred_at.date() for e in self.evidence if e.kind == EventKind.HOLD_RELEASED
        ]
        if not placed:
            return set()

        start = min(placed)
        end = max(released) if released else start
        out: set[date] = set()
        cursor = start
        while cursor <= end:
            out.add(cursor)
            cursor += timedelta(days=1)
        return out
