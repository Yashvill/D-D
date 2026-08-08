"""DisputeArc: one instance per invoice. Days 40-90+.

Spawned with ``ParentClosePolicy.ABANDON`` so it outlives its parent by weeks.
This is the phase no session-shaped system can reach at all: the container was
returned on day 23 and the money arrives on day 65.

The dispute window cannot quietly expire - not because anyone remembers, but
because a timer set on day 40 fires on day 70 whether the team remembers or not.
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
        AuditResult,
        ChargeType,
        ContractTerms,
        DisputeLetter,
        Evidence,
        EvidenceChronology,
        Invoice,
        LfdCalculation,
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

# 30 days to dispute, 30 to resolve. The hard backstop sits at 30 days past
# issuance; we file far earlier than that.
DISPUTE_WINDOW_DAYS = 30
FOLLOW_UP_INTERVAL = timedelta(days=5)
MAX_FOLLOW_UPS = 6


class DisputeResult(BaseModel):
    invoice_id: str
    container_id: str
    filed: bool = False
    case_ref: str = ""
    amount_contested_usd: Decimal = Decimal("0")
    amount_recovered_usd: Decimal = Decimal("0")
    findings: list[str] = Field(default_factory=list)
    escalated: bool = False
    follow_ups_sent: int = 0
    outcome: str = "unresolved"


class DisputeInput(BaseModel):
    container_id: str
    invoice: Invoice
    terms: ContractTerms
    evidence: list[Evidence] = Field(default_factory=list)
    lfd: LfdCalculation | None = None
    clock_start: date | None = None
    clock_end: date | None = None
    exam_days_usd: Decimal = Decimal("0")
    capacity_days_usd: Decimal = Decimal("0")
    ungrounded_days_usd: Decimal = Decimal("0")
    settlement_mandate_fraction: Decimal = Decimal("0.70")


@workflow.defn
class DisputeArc:
    """One invoice, audited, contested and prosecuted to settlement."""

    def __init__(self) -> None:
        self.audit: AuditResult | None = None
        self.chronology: EvidenceChronology | None = None
        self.letter: DisputeLetter | None = None
        self.case_ref = ""
        self.filed_at: datetime | None = None
        self.follow_ups = 0
        self.offer_usd: Decimal | None = None
        self.recovered_usd = Decimal("0")
        self.escalated = False
        self.approvals: set[str] = set()
        self.carrier_reply: str = ""
        self.resolved = False
        self.outcome = "unresolved"

    # ---------------- signals ----------------

    @workflow.signal
    def carrier_replied(self, message: str, offer_usd: Decimal | None = None) -> None:
        """Advances or closes the case. Raced against the follow-up timer."""
        self.carrier_reply = message
        if offer_usd is not None:
            self.offer_usd = offer_usd

    @workflow.signal
    def approve_settlement(self, action: str) -> None:
        self.approvals.add(action)

    @workflow.signal
    def settled(self, amount_usd: Decimal) -> None:
        self.recovered_usd = amount_usd
        self.resolved = True
        self.outcome = "settled"

    # ---------------- queries ----------------

    @workflow.query
    def findings(self) -> list[str]:
        """Queried by the demo to show the Part 541 defect that was caught."""
        return self.audit.codes if self.audit else []

    @workflow.query
    def state(self) -> dict:
        return {
            "invoice_id": self.letter.invoice_id if self.letter else "",
            "filed": self.filed_at is not None,
            "filed_at": self.filed_at.isoformat() if self.filed_at else None,
            "case_ref": self.case_ref,
            "findings": self.audit.codes if self.audit else [],
            "voids_entire_charge": self.audit.voids_entire_charge if self.audit else False,
            "amount_contested_usd": (
                str(self.letter.amount_contested_usd) if self.letter else "0"
            ),
            "claims": len(self.chronology.claims) if self.chronology else 0,
            "dropped_claims": self.chronology.dropped_claims if self.chronology else [],
            "follow_ups_sent": self.follow_ups,
            "offer_usd": str(self.offer_usd) if self.offer_usd is not None else None,
            "recovered_usd": str(self.recovered_usd),
            "escalated": self.escalated,
            "outcome": self.outcome,
        }

    # ---------------- run ----------------

    @workflow.run
    async def run(self, inp: DisputeInput) -> DisputeResult:
        invoice = inp.invoice

        # 1. Audit. The cheapest money in the system.
        self.audit = await workflow.execute_activity(
            act.audit_invoice,
            act.AuditInvoiceInput(
                invoice=invoice,
                evidence=inp.evidence,
                audited_at=_now(),
                terms=inp.terms,
                clock_start=inp.clock_start,
                clock_end=inp.clock_end,
                effective_lfd=inp.lfd.effective_lfd if inp.lfd else None,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

        # 2. Assemble the cited chronology. Uncited claims die here.
        self.chronology = await workflow.execute_activity(
            act.build_evidence_chronology,
            act.AssembleEvidenceInput(
                container_id=inp.container_id,
                invoice_id=invoice.invoice_id,
                assembled_at=_now(),
                evidence=inp.evidence,
                audit=self.audit,
                lfd=inp.lfd,
                charge_type=invoice.charge_type,
                exam_days_usd=inp.exam_days_usd,
                capacity_days_usd=inp.capacity_days_usd,
                ungrounded_days_usd=inp.ungrounded_days_usd,
                billed_total_usd=invoice.total_usd,
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

        if not self.chronology.claims:
            # Nothing defensible to say. Saying it anyway is how credibility dies.
            self.outcome = "no_grounds"
            return self._result(inp)

        # 3. Draft. LLM sees only provable material.
        self.letter = await workflow.execute_activity(
            act.draft_dispute_letter,
            act.DraftDisputeInput(
                chronology=self.chronology,
                billing_party=invoice.billing_party,
                total_billed_usd=invoice.total_usd,
                drafted_at=_now(),
            ),
            start_to_close_timeout=LONG,
            retry_policy=LLM_RETRY,
        )

        # 4. File, within hours of receipt rather than weeks.
        submission = await workflow.execute_activity(
            act.submit_dispute,
            act.SubmitDisputeInput(
                letter=self.letter,
                billing_party=invoice.billing_party,
                submitted_at=_now(),
            ),
            start_to_close_timeout=LONG,
            retry_policy=IO_RETRY,
        )
        self.case_ref = submission.case_ref
        self.filed_at = submission.submitted_at

        # 5. Follow-up cadence raced against a reply, with a hard deadline.
        await self._prosecute(inp)

        return self._result(inp)

    # ---------------- internals ----------------

    async def _prosecute(self, inp: DisputeInput) -> None:
        """Chase on a cadence nobody has to remember."""
        deadline = datetime.combine(
            inp.invoice.issued_at + timedelta(days=DISPUTE_WINDOW_DAYS),
            datetime.min.time(),
        )

        while not self.resolved and self.follow_ups < MAX_FOLLOW_UPS:
            try:
                await workflow.wait_condition(
                    lambda: self.resolved or bool(self.carrier_reply) or self.offer_usd is not None,
                    timeout=FOLLOW_UP_INTERVAL,
                )
            except asyncio.TimeoutError:
                self.follow_ups += 1
                continue

            if self.offer_usd is not None:
                await self._evaluate_offer(inp)
                if self.resolved:
                    return
                self.offer_usd = None
                self.carrier_reply = ""
            elif self.carrier_reply:
                # Acknowledgement or a request for more documentation. The
                # evidence is already stored, so answering costs nothing.
                self.carrier_reply = ""

            if _now() >= deadline and not self.resolved:
                break

        if not self.resolved:
            await self._escalate("follow-up cadence exhausted or window closing")

    async def _evaluate_offer(self, inp: DisputeInput) -> None:
        """Bounded settlement authority, enforced in workflow code."""
        assert self.letter is not None
        offer = self.offer_usd or Decimal("0")
        claim = self.letter.amount_contested_usd

        from agents.case_builder.draft_dispute import evaluate_settlement

        may_accept, reason = evaluate_settlement(
            offer, claim, mandate_fraction=inp.settlement_mandate_fraction
        )

        if may_accept:
            self.recovered_usd = offer
            self.resolved = True
            self.outcome = "settled_within_mandate"
            return

        await self._escalate(reason)
        # Park indefinitely until a human rules on it.
        await workflow.wait_condition(lambda: "settlement" in self.approvals or self.resolved)
        if "settlement" in self.approvals and not self.resolved:
            self.recovered_usd = offer
            self.resolved = True
            self.outcome = "settled_by_human"

    async def _escalate(self, reason: str) -> None:
        self.escalated = True
        await workflow.execute_activity(
            act.notify_human,
            act.NotifyHumanInput(
                container_id=self.chronology.container_id if self.chronology else "",
                reason=reason,
                action="settlement",
                cost_usd=Decimal("0"),
                detail=f"case {self.case_ref}",
            ),
            start_to_close_timeout=SHORT,
            retry_policy=IO_RETRY,
        )

    def _result(self, inp: DisputeInput) -> DisputeResult:
        return DisputeResult(
            invoice_id=inp.invoice.invoice_id,
            container_id=inp.container_id,
            filed=self.filed_at is not None,
            case_ref=self.case_ref,
            amount_contested_usd=(
                self.letter.amount_contested_usd if self.letter else Decimal("0")
            ),
            amount_recovered_usd=self.recovered_usd,
            findings=self.audit.codes if self.audit else [],
            escalated=self.escalated,
            follow_ups_sent=self.follow_ups,
            outcome=self.outcome,
        )
