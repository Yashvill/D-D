"""Pydantic domain models for Persistent Fleet.

These models cross the Temporal activity boundary, which means the
``pydantic_data_converter`` MUST be configured on the client, the worker and
the test environment. ``date`` and ``datetime`` fields only serialise with that
converter.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChargeType(str, Enum):
    DEMURRAGE = "demurrage"
    DETENTION = "detention"
    STORAGE = "storage"
    CHASSIS = "chassis"


class RiskLevel(str, Enum):
    PENDING = "PENDING"
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    CLOSED = "CLOSED"
    DISPUTED = "DISPUTED"


class HoldType(str, Enum):
    CUSTOMS_EXAM = "customs_exam"
    VACIS = "vacis"
    FREIGHT = "freight"
    USDA = "usda"


class EventKind(str, Enum):
    CUSTOMS_ENTRY_FILED = "customs_entry_filed"
    DISCHARGED = "discharged"
    CONTAINER_AVAILABLE = "container_available"
    AVAILABILITY_MISS = "availability_miss"
    HOLD_PLACED = "hold_placed"
    HOLD_RELEASED = "hold_released"
    APPOINTMENT_UNAVAILABLE = "appointment_unavailable"
    APPOINTMENT_BOOKED = "appointment_booked"
    GATE_OUT = "gate_out"
    CARGO_STRIPPED = "cargo_stripped"
    CARRIER_ADVISORY = "carrier_advisory"
    EMPTY_RETURNED = "empty_returned"
    INVOICE_RECEIVED = "invoice_received"
    CARRIER_REPLIED = "carrier_replied"
    APPROVAL_GRANTED = "approval_granted"
    NEAR_MISS = "near_miss"


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Contract terms
# --------------------------------------------------------------------------


class Tier(Base):
    """One band of an escalating rate schedule.

    ``from_day`` is inclusive and 1-based, counted from the first billed day.
    ``to_day`` is inclusive; ``None`` means "and everything after".
    """

    from_day: int = Field(ge=1)
    to_day: Optional[int] = Field(default=None, ge=1)
    rate_usd: Decimal = Field(ge=0)

    @field_validator("to_day")
    @classmethod
    def _to_after_from(cls, v: Optional[int], info):
        start = info.data.get("from_day")
        if v is not None and start is not None and v < start:
            raise ValueError("to_day must be >= from_day")
        return v

    def covers(self, billed_day: int) -> bool:
        if billed_day < self.from_day:
            return False
        return self.to_day is None or billed_day <= self.to_day


class ClockTerms(Base):
    """Free time plus tier table for a single charge type."""

    charge_type: ChargeType
    billed_by: str
    free_days: int = Field(ge=0)
    tiers: list[Tier]
    counts_weekends: bool = True
    counts_holidays: bool = True

    def rate_for(self, billed_day: int) -> Decimal:
        for tier in self.tiers:
            if tier.covers(billed_day):
                return tier.rate_usd
        return self.tiers[-1].rate_usd if self.tiers else Decimal("0")


class ContractTerms(Base):
    """Everything extracted from the service contract and terminal tariff.

    This is the single highest-leverage object in the system: every later
    calculation depends on it. ``confidence`` is emitted by the extraction
    activity so low-confidence extractions can be routed to a human on day 1,
    when there is no time pressure.
    """

    contract_id: str
    port: str
    carrier: str
    clocks: dict[ChargeType, ClockTerms]
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    source_document_id: Optional[str] = None
    extraction_notes: list[str] = Field(default_factory=list)

    def clock(self, charge_type: ChargeType) -> ClockTerms:
        if charge_type not in self.clocks:
            raise KeyError(f"no terms loaded for {charge_type.value}")
        return self.clocks[charge_type]


# --------------------------------------------------------------------------
# Identity and events
# --------------------------------------------------------------------------


class ContainerInput(Base):
    """Immutable identity of the container the workflow is built around."""

    container_id: str
    contract_id: str
    port: str
    terminal: str
    carrier: str
    consignee: str
    return_depot: str
    bill_of_lading: str

    @property
    def workflow_id(self) -> str:
        return f"container::{self.container_id}"


class Evidence(Base):
    """A fact recorded with its provenance.

    ``source_document_id`` is mandatory. An event without one is a rumour and
    is dropped at case-assembly time rather than argued about in a prompt.
    """

    kind: EventKind
    occurred_at: datetime
    source_system: str
    source_document_id: str
    summary: str
    detail: dict[str, str] = Field(default_factory=dict)

    @field_validator("source_document_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("source_document_id may not be empty")
        return v.strip()

    @field_validator("occurred_at")
    @classmethod
    def _naive_utc(cls, v: datetime) -> datetime:
        """Normalise to tz-naive UTC.

        ``workflow.now()`` yields tz-aware UTC datetimes while external events use
        naive ones; mixing the two makes any comparison (e.g. sorting the
        chronology) raise ``can't compare offset-naive and offset-aware``.
        """
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class ContainerEvent(Base):
    """A normalised inbound event, before it becomes evidence."""

    kind: EventKind
    occurred_at: datetime
    source_system: str
    source_document_id: Optional[str] = None
    payload: dict[str, str] = Field(default_factory=dict)

    @property
    def is_citable(self) -> bool:
        return bool(self.source_document_id and self.source_document_id.strip())

    def to_evidence(self, summary: str) -> Evidence:
        if not self.is_citable:
            raise ValueError(f"{self.kind.value} has no source_document_id")
        return Evidence(
            kind=self.kind,
            occurred_at=self.occurred_at,
            source_system=self.source_system,
            source_document_id=self.source_document_id,  # type: ignore[arg-type]
            summary=summary,
            detail=dict(self.payload),
        )


class Hold(Base):
    hold_type: HoldType
    placed_at: datetime
    released_at: Optional[datetime] = None
    reference: str = ""

    @property
    def is_open(self) -> bool:
        return self.released_at is None


# --------------------------------------------------------------------------
# Strategist output
# --------------------------------------------------------------------------


class Option(Base):
    """One row of the costed options table."""

    action: str
    cost_usd: Decimal = Field(ge=0)
    p_success: float = Field(ge=0.0, le=1.0)
    gross_saving_usd: Decimal = Field(ge=0)
    note: str = ""
    requires_approval: bool = False

    @property
    def expected_saving_usd(self) -> Decimal:
        """Expected value net of the cost of acting."""
        return (self.gross_saving_usd * Decimal(str(self.p_success))) - self.cost_usd


class OptionsTable(Base):
    container_id: str
    assessed_at: datetime
    options: list[Option]

    def best(self) -> Option:
        return max(self.options, key=lambda o: o.expected_saving_usd)


class RiskAssessment(Base):
    """Rule-based risk view plus the stored counterfactual.

    ``counterfactual_usd`` is what the agent predicts will be billed if it does
    nothing. It is scored against reality later, which is what makes the
    savings claim auditable rather than merely asserted.
    """

    container_id: str
    assessed_at: datetime
    level: RiskLevel
    reason: str
    counterfactual_usd: Decimal = Field(ge=0)
    days_to_lfd: Optional[int] = None
    intervention_suppressed: bool = False
    suppression_reason: str = ""


class LfdCalculation(Base):
    """Leak 01: the gap between the nominal and the effective last free day."""

    nominal_lfd: date
    effective_lfd: date
    ungrounded_days: int = Field(ge=0)
    discharged_at: datetime
    available_at: Optional[datetime] = None
    citations: list[str] = Field(default_factory=list)

    @property
    def shifted(self) -> bool:
        return self.effective_lfd != self.nominal_lfd


# --------------------------------------------------------------------------
# Invoices and dispute
# --------------------------------------------------------------------------


class InvoiceLine(Base):
    charge_type: ChargeType
    billed_day: int = Field(ge=1)
    charge_date: date
    rate_usd: Decimal = Field(ge=0)
    amount_usd: Decimal = Field(ge=0)


class Invoice(Base):
    """A received D&D invoice, as parsed.

    The ``*_present`` flags mirror the mandatory content categories of
    46 CFR 541.6. Their absence is what the Auditor turns into money.
    """

    invoice_id: str
    charge_type: ChargeType
    billing_party: str
    container_id: str
    issued_at: date
    last_charge_incurred_at: date
    total_usd: Decimal = Field(ge=0)
    lines: list[InvoiceLine] = Field(default_factory=list)
    free_time_claimed_days: Optional[int] = None
    lfd_claimed: Optional[date] = None

    identifying_info_present: bool = True
    timing_info_present: bool = True
    rate_info_present: bool = True
    dispute_info_present: bool = True
    certification_present: bool = True

    source_document_id: str = ""


class FindingCode(str, Enum):
    LATE_ISSUANCE = "late_issuance"
    IDENTIFYING_INFO_ABSENT = "identifying_info_absent"
    TIMING_INFO_ABSENT = "timing_info_absent"
    RATE_INFO_ABSENT = "rate_info_absent"
    DISPUTE_INFO_ABSENT = "dispute_info_absent"
    CERTIFICATION_ABSENT = "certification_absent"
    CERTIFICATION_CONTRADICTED = "certification_contradicted"
    TIER_MISAPPLIED = "tier_misapplied"
    FREE_TIME_WRONG = "free_time_wrong"
    DOUBLE_BILLED = "double_billed"


class Finding(Base):
    code: FindingCode
    basis: str
    detail: str
    voids_charge: bool = False
    recompute_delta_usd: Decimal = Decimal("0")


class AuditResult(Base):
    invoice_id: str
    audited_at: datetime
    findings: list[Finding] = Field(default_factory=list)
    recomputed_total_usd: Decimal = Field(default=Decimal("0"), ge=0)
    billed_total_usd: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def voids_entire_charge(self) -> bool:
        return any(f.voids_charge for f in self.findings)

    @property
    def overbilled_usd(self) -> Decimal:
        delta = self.billed_total_usd - self.recomputed_total_usd
        return delta if delta > 0 else Decimal("0")

    @property
    def codes(self) -> list[str]:
        return [f.code.value for f in self.findings]


class Claim(Base):
    """A single assertion in a dispute, with its supporting citations.

    ``voids_whole_charge`` marks a claim that defeats the entire invoice on its
    own - typically a Part 541 content omission. Such a claim is not additive
    with the day-level claims; they are pleaded as alternatives to it.
    """

    heading: str
    argument: str
    amount_usd: Decimal = Field(ge=0)
    citations: list[str] = Field(default_factory=list)
    voids_whole_charge: bool = False

    @property
    def is_supported(self) -> bool:
        return len(self.citations) > 0


class EvidenceChronology(Base):
    """The assembled case for one invoice.

    ``billed_total_usd`` is a hard ceiling. Distinct legal grounds often overlap
    on the same dollars - a missing certification voids the whole charge, and the
    day-level arguments are alternatives to it, not additions. Summing them
    naively produces a claim larger than the invoice, which is the fastest way to
    lose credibility with both a carrier and a judge.
    """

    container_id: str
    invoice_id: str
    assembled_at: datetime
    billed_total_usd: Decimal = Field(default=Decimal("0"), ge=0)
    claims: list[Claim] = Field(default_factory=list)
    dropped_claims: list[str] = Field(default_factory=list)
    timeline: list[Evidence] = Field(default_factory=list)

    @property
    def gross_claims_usd(self) -> Decimal:
        """Sum of every ground pleaded, before the overlap ceiling."""
        return sum((c.amount_usd for c in self.claims), Decimal("0"))

    @property
    def total_claimed_usd(self) -> Decimal:
        """The amount actually contested, never more than was invoiced."""
        if self.billed_total_usd <= 0:
            return self.gross_claims_usd
        return min(self.gross_claims_usd, self.billed_total_usd)

    @property
    def has_voiding_claim(self) -> bool:
        return any(c.voids_whole_charge for c in self.claims)


class DisputeLetter(Base):
    invoice_id: str
    container_id: str
    subject: str
    body: str
    amount_contested_usd: Decimal = Field(ge=0)
    citations: list[str] = Field(default_factory=list)
    drafted_at: datetime


# --------------------------------------------------------------------------
# Negotiator results
# --------------------------------------------------------------------------


class Booking(Base):
    booking_id: str
    kind: str
    slot_at: datetime
    provider: str
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)


class AppointmentAttempt(Base):
    """The result of one slot scan.

    A miss is not an error. It is a successful observation of an adverse fact
    and belongs in the evidence list, not the retry log.
    """

    attempted_at: datetime
    terminal: str
    slot_at: Optional[datetime] = None
    reference: str = ""

    @property
    def succeeded(self) -> bool:
        return self.slot_at is not None


class AdvisoryClassification(Base):
    """LLM output: does this carrier advisory affect this container?"""

    affects_this_container: bool
    depot: str
    restriction_starts: Optional[date] = None
    restriction_ends: Optional[date] = None
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


class CounterfactualScore(Base):
    container_id: str
    scored_at: datetime
    predicted_usd: Decimal
    actual_usd: Decimal
    intervention: str
    prevented_usd: Decimal

    @property
    def error_usd(self) -> Decimal:
        return abs(self.predicted_usd - self.actual_usd)
