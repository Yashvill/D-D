"""Agent functions wrapped as Temporal activities.

This is the determinism boundary. Everything non-deterministic lives on this side
of it: LLM calls, network I/O, clock reads, randomness. Workflow code only
orchestrates.

Getting this boundary right is the difference between "used Temporal" and
"understood Temporal", and it is the thing a judge who knows Temporal will probe.

Activities take a single Pydantic model as input so the payload is versionable
and self-describing in event history.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field
from temporalio import activity

from agents.auditor import check_part_541, recompute_invoice
from agents.case_builder import assemble_evidence, draft_dispute
from agents.negotiator import book_drayage, reserve_appointment
from agents.shared.models import (
    AdvisoryClassification,
    AppointmentAttempt,
    AuditResult,
    Booking,
    ChargeType,
    ContainerInput,
    ContractTerms,
    DisputeLetter,
    Evidence,
    EvidenceChronology,
    Finding,
    Hold,
    Invoice,
    LfdCalculation,
    OptionsTable,
    RiskAssessment,
)
from agents.strategist import assess_risk, effective_lfd, price_options
from agents.watcher import classify_advisory, extract_terms

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Activity input models
# --------------------------------------------------------------------------


class LoadTermsInput(BaseModel):
    contract_id: str
    port: str
    carrier: str
    contract_text: str = ""
    contract_pdf_path: Optional[str] = None
    source_document_id: str


class ClassifyAdvisoryInput(BaseModel):
    advisory_text: str
    depot: str
    carrier: str
    container_type: str = "40HC"


class EffectiveLfdInput(BaseModel):
    terms: ContractTerms
    discharged_at: datetime
    available_at: Optional[datetime] = None
    availability_misses: list[Evidence] = Field(default_factory=list)


class AssessRiskInput(BaseModel):
    container_id: str
    terms: ContractTerms
    as_of: datetime
    effective_lfd: date
    clock_start: date
    holds: list[Hold] = Field(default_factory=list)
    projected_end: date
    gated_out: bool = False


class PriceDemurrageInput(BaseModel):
    container_id: str
    as_of: datetime
    exposure_usd: Decimal
    intervention_suppressed: bool
    suppression_reason: str = ""
    slot_scarcity_prior: Optional[float] = None
    auto_approve_limit_usd: Decimal = Decimal("250")


class PriceDetentionInput(BaseModel):
    container_id: str
    as_of: datetime
    terms: ContractTerms
    gated_out_at: date
    projected_return: date
    chassis_start: Optional[date] = None
    depot_restriction_prior: float = 0.0
    auto_approve_limit_usd: Decimal = Decimal("250")


class ReserveAppointmentInput(BaseModel):
    container_id: str
    terminal: str
    attempted_at: datetime
    slot_available: bool
    slot_at: Optional[datetime] = None


class BookDrayageInput(BaseModel):
    container_id: str
    provider: str
    slot_at: datetime
    cost_usd: Decimal = Decimal("0")
    expedited: bool = False


class BookEmptyReturnInput(BaseModel):
    container_id: str
    depot: str
    slot_at: datetime


class AuditInvoiceInput(BaseModel):
    invoice: Invoice
    evidence: list[Evidence] = Field(default_factory=list)
    audited_at: datetime
    terms: Optional[ContractTerms] = None
    clock_start: Optional[date] = None
    clock_end: Optional[date] = None
    effective_lfd: Optional[date] = None


class AssembleEvidenceInput(BaseModel):
    container_id: str
    invoice_id: str
    assembled_at: datetime
    evidence: list[Evidence] = Field(default_factory=list)
    audit: Optional[AuditResult] = None
    lfd: Optional[LfdCalculation] = None
    charge_type: ChargeType = ChargeType.DEMURRAGE
    exam_days_usd: Decimal = Decimal("0")
    capacity_days_usd: Decimal = Decimal("0")
    ungrounded_days_usd: Decimal = Decimal("0")
    billed_total_usd: Decimal = Decimal("0")


class DraftDisputeInput(BaseModel):
    chronology: EvidenceChronology
    billing_party: str
    total_billed_usd: Decimal
    drafted_at: datetime


class NotifyHumanInput(BaseModel):
    container_id: str
    reason: str
    action: str
    cost_usd: Decimal = Decimal("0")
    detail: str = ""


class SubmitDisputeInput(BaseModel):
    letter: DisputeLetter
    billing_party: str
    submitted_at: datetime


class SubmitDisputeResult(BaseModel):
    case_ref: str
    submitted_at: datetime


# --------------------------------------------------------------------------
# Watcher
# --------------------------------------------------------------------------


@activity.defn
async def load_contract_terms(inp: LoadTermsInput) -> ContractTerms:
    """LLM. Parse the service contract into structured terms."""
    text = inp.contract_text
    if inp.contract_pdf_path:
        text = extract_terms.read_contract_pdf(inp.contract_pdf_path)

    return extract_terms.extract_terms(
        text,
        port=inp.port,
        carrier=inp.carrier,
        contract_id=inp.contract_id,
        source_document_id=inp.source_document_id,
    )


@activity.defn
async def classify_carrier_advisory(inp: ClassifyAdvisoryInput) -> AdvisoryClassification:
    """LLM. Decide whether an advisory hits this container's depot."""
    return classify_advisory.classify_advisory(
        inp.advisory_text,
        depot=inp.depot,
        carrier=inp.carrier,
        container_type=inp.container_type,
    )


# --------------------------------------------------------------------------
# Strategist - all rule-based
# --------------------------------------------------------------------------


@activity.defn
async def compute_effective_lfd(inp: EffectiveLfdInput) -> LfdCalculation:
    return effective_lfd.compute_effective_lfd(
        inp.terms.clock(ChargeType.DEMURRAGE),
        inp.discharged_at,
        inp.available_at,
        inp.availability_misses,
    )


@activity.defn
async def assess_container_risk(inp: AssessRiskInput) -> RiskAssessment:
    return assess_risk.assess(
        inp.container_id,
        inp.terms,
        as_of=inp.as_of,
        effective_lfd=inp.effective_lfd,
        clock_start=inp.clock_start,
        holds=inp.holds,
        projected_end=inp.projected_end,
        gated_out=inp.gated_out,
    )


@activity.defn
async def price_demurrage_options(inp: PriceDemurrageInput) -> OptionsTable:
    return price_options.price_demurrage_options(
        inp.container_id,
        as_of=inp.as_of,
        exposure_usd=inp.exposure_usd,
        intervention_suppressed=inp.intervention_suppressed,
        suppression_reason=inp.suppression_reason,
        slot_scarcity_prior=inp.slot_scarcity_prior,
        auto_approve_limit_usd=inp.auto_approve_limit_usd,
    )


@activity.defn
async def price_detention_options(inp: PriceDetentionInput) -> OptionsTable:
    exposure = assess_risk.detention_exposure(
        inp.terms,
        inp.gated_out_at,
        inp.projected_return,
        chassis_start=inp.chassis_start,
    )
    return price_options.price_detention_options(
        inp.container_id,
        as_of=inp.as_of,
        detention_exposure_usd=exposure,
        depot_restriction_prior=inp.depot_restriction_prior,
        auto_approve_limit_usd=inp.auto_approve_limit_usd,
    )


# --------------------------------------------------------------------------
# Negotiator
# --------------------------------------------------------------------------


@activity.defn
async def reserve_terminal_appointment(inp: ReserveAppointmentInput) -> AppointmentAttempt:
    """Never raises on a miss. A miss is evidence, not an error."""
    return reserve_appointment.reserve_appointment(
        inp.container_id,
        inp.terminal,
        attempted_at=inp.attempted_at,
        slot_available=inp.slot_available,
        slot_at=inp.slot_at,
    )


@activity.defn
async def book_container_drayage(inp: BookDrayageInput) -> Booking:
    return book_drayage.book_drayage(
        inp.container_id,
        provider=inp.provider,
        slot_at=inp.slot_at,
        cost_usd=inp.cost_usd,
        expedited=inp.expedited,
    )


@activity.defn
async def cancel_container_drayage(booking: Booking) -> str:
    """Saga compensator."""
    return book_drayage.cancel_drayage(booking)


@activity.defn
async def book_container_empty_return(inp: BookEmptyReturnInput) -> Booking:
    """The decisive action. Costs nothing, prevents the whole second clock."""
    return book_drayage.book_empty_return(
        inp.container_id, depot=inp.depot, slot_at=inp.slot_at
    )


@activity.defn
async def cancel_container_empty_return(booking: Booking) -> str:
    """Saga compensator."""
    return book_drayage.cancel_empty_return(booking)


@activity.defn
async def release_terminal_appointment(booking: Booking) -> str:
    """Saga compensator. Releases a slot rather than abandoning it."""
    return reserve_appointment.release_appointment(booking)


# --------------------------------------------------------------------------
# Auditor
# --------------------------------------------------------------------------


@activity.defn
async def audit_invoice(inp: AuditInvoiceInput) -> AuditResult:
    """Run the Part 541 checklist and recompute from the contract."""
    recomputed = None
    extra: list[Finding] = []

    if inp.terms and inp.clock_start and inp.clock_end:
        recomputed, extra = recompute_invoice.recompute(
            inp.invoice,
            inp.terms,
            clock_start=inp.clock_start,
            clock_end=inp.clock_end,
            effective_lfd=inp.effective_lfd,
        )

    return check_part_541.audit(
        inp.invoice,
        inp.evidence,
        audited_at=inp.audited_at,
        recomputed_total_usd=recomputed,
        extra_findings=extra,
    )


# --------------------------------------------------------------------------
# Case Builder
# --------------------------------------------------------------------------


@activity.defn
async def build_evidence_chronology(inp: AssembleEvidenceInput) -> EvidenceChronology:
    """Drops any claim lacking a citation, before the LLM ever sees it."""
    return assemble_evidence.assemble(
        inp.container_id,
        inp.invoice_id,
        assembled_at=inp.assembled_at,
        evidence=inp.evidence,
        audit=inp.audit,
        lfd=inp.lfd,
        charge_type=inp.charge_type,
        exam_days_usd=inp.exam_days_usd,
        capacity_days_usd=inp.capacity_days_usd,
        ungrounded_days_usd=inp.ungrounded_days_usd,
        billed_total_usd=inp.billed_total_usd,
    )


@activity.defn
async def draft_dispute_letter(inp: DraftDisputeInput) -> DisputeLetter:
    """LLM. Only cited material reaches this point."""
    return draft_dispute.draft(
        inp.chronology,
        billing_party=inp.billing_party,
        total_billed_usd=inp.total_billed_usd,
        drafted_at=inp.drafted_at,
    )


@activity.defn
async def submit_dispute(inp: SubmitDisputeInput) -> SubmitDisputeResult:
    """Mocked filing. In production a human submits above threshold."""
    log.info(
        "filing dispute for invoice %s contesting $%s",
        inp.letter.invoice_id,
        inp.letter.amount_contested_usd,
    )
    return SubmitDisputeResult(
        case_ref=f"case::{inp.letter.invoice_id}",
        submitted_at=inp.submitted_at,
    )


# --------------------------------------------------------------------------
# Human in the loop
# --------------------------------------------------------------------------


@activity.defn
async def notify_human(inp: NotifyHumanInput) -> str:
    """Log-and-display rather than send, for the demo."""
    log.warning(
        "APPROVAL NEEDED  %s  action=%s cost=$%s  reason=%s",
        inp.container_id,
        inp.action,
        inp.cost_usd,
        inp.reason,
    )
    return f"notified::{inp.container_id}::{inp.action}"


ALL_ACTIVITIES = [
    load_contract_terms,
    classify_carrier_advisory,
    compute_effective_lfd,
    assess_container_risk,
    price_demurrage_options,
    price_detention_options,
    reserve_terminal_appointment,
    book_container_drayage,
    cancel_container_drayage,
    book_container_empty_return,
    cancel_container_empty_return,
    release_terminal_appointment,
    audit_invoice,
    build_evidence_chronology,
    draft_dispute_letter,
    submit_dispute,
    notify_human,
]
