"""Assemble the cited evidence chronology.

The guardrail that matters most in the whole system lives here, and it is
enforced in code rather than requested in a prompt:

    No source_document_id, no claim.

An uncited claim is dropped at assembly time. It never reaches the LLM, so the
model cannot be talked into reinstating it, and the dispute letter cannot assert
a fact we cannot prove. This is the difference between a document and an
argument.

The case is assembled *before* any invoice arrives. By the time the invoice
lands, the chronology is already built and the dispute can be filed within 24
hours instead of entering an approval queue.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from agents.shared.models import (
    AuditResult,
    ChargeType,
    Claim,
    Evidence,
    EventKind,
    EvidenceChronology,
    FindingCode,
    LfdCalculation,
)

log = logging.getLogger(__name__)


def assemble(
    container_id: str,
    invoice_id: str,
    *,
    assembled_at: datetime,
    evidence: list[Evidence],
    audit: AuditResult | None = None,
    lfd: LfdCalculation | None = None,
    charge_type: ChargeType = ChargeType.DEMURRAGE,
    exam_days_usd: Decimal = Decimal("0"),
    capacity_days_usd: Decimal = Decimal("0"),
    ungrounded_days_usd: Decimal = Decimal("0"),
    billed_total_usd: Decimal = Decimal("0"),
) -> EvidenceChronology:
    """Build the chronology, dropping anything that cannot be cited.

    Returns a chronology whose ``dropped_claims`` field names every argument that
    was discarded for want of a citation. Showing the drops is what makes the
    system auditable rather than merely confident.

    ``billed_total_usd`` caps the contested amount. Grounds overlap on the same
    dollars, so the sum of them is a pleading, not an invoice.
    """
    candidates: list[Claim] = []

    if audit is not None:
        candidates.extend(_regulatory_claims(audit))

    # Day-level grounds are pleaded in the alternative to any voiding finding.
    alternative = " Pleaded in the alternative to the grounds above." if candidates else ""

    if lfd is not None and lfd.shifted and ungrounded_days_usd > 0:
        candidates.append(
            Claim(
                heading="Effective last free day understated",
                argument=(
                    f"The container was discharged on "
                    f"{lfd.discharged_at.date().isoformat()} but was not grounded or "
                    f"appointable until "
                    f"{lfd.available_at.date().isoformat() if lfd.available_at else 'later'}, "
                    f"consuming {lfd.ungrounded_days} of the contracted free days "
                    f"through the terminal's own handling. The last free day should "
                    f"be {lfd.effective_lfd.isoformat()}, not "
                    f"{lfd.nominal_lfd.isoformat()}." + alternative
                ),
                amount_usd=ungrounded_days_usd,
                citations=list(lfd.citations),
            )
        )

    capacity_citations = _citations_for(evidence, EventKind.APPOINTMENT_UNAVAILABLE)
    if capacity_days_usd > 0:
        candidates.append(
            Claim(
                heading="Charges caused by the terminal's own capacity failure",
                argument=(
                    f"Across {len(capacity_citations)} recorded attempts the "
                    f"terminal's appointment system offered no slot. These days were "
                    f"billed at the highest tier and were caused by the billing "
                    f"party's own performance." + alternative
                ),
                amount_usd=capacity_days_usd,
                citations=capacity_citations,
            )
        )

    hold_citations = _citations_for(evidence, EventKind.HOLD_PLACED, EventKind.HOLD_RELEASED)
    if exam_days_usd > 0:
        candidates.append(
            Claim(
                heading="Charges accrued while the container was legally immovable",
                argument=(
                    "The container was under a customs examination hold and routed "
                    "to a Centralised Examination Station. No action was available "
                    "to the billed party during this window, and a charge that "
                    "cannot change behaviour cannot operate as an incentive."
                    + alternative
                ),
                amount_usd=exam_days_usd,
                citations=hold_citations,
            )
        )

    # The guardrail. Enforced here, before the LLM ever sees the material.
    kept: list[Claim] = []
    dropped: list[str] = []

    for claim in candidates:
        if claim.is_supported:
            kept.append(claim)
        else:
            dropped.append(claim.heading)
            log.warning("dropping uncited claim: %s", claim.heading)

    return EvidenceChronology(
        container_id=container_id,
        invoice_id=invoice_id,
        assembled_at=assembled_at,
        billed_total_usd=billed_total_usd or (audit.billed_total_usd if audit else Decimal("0")),
        claims=kept,
        dropped_claims=dropped,
        timeline=sorted(evidence, key=lambda e: e.occurred_at),
    )


def _regulatory_claims(audit: AuditResult) -> list[Claim]:
    """Turn audit findings into claims.

    Regulatory findings are self-citing: the invoice itself is the document, so
    these always carry a citation.
    """
    claims: list[Claim] = []

    for finding in audit.findings:
        if finding.voids_charge:
            amount = audit.billed_total_usd
        elif finding.recompute_delta_usd > 0:
            amount = finding.recompute_delta_usd
        elif finding.code == FindingCode.CERTIFICATION_CONTRADICTED:
            amount = Decimal("0")  # supports other claims rather than standing alone
        else:
            continue

        claims.append(
            Claim(
                heading=_heading_for(finding.code),
                argument=finding.detail,
                amount_usd=amount,
                citations=[f"invoice::{audit.invoice_id}", finding.basis],
                voids_whole_charge=finding.voids_charge,
            )
        )

    return claims


def _heading_for(code: FindingCode) -> str:
    return {
        FindingCode.LATE_ISSUANCE: "Invoice issued outside the 30-day window",
        FindingCode.CERTIFICATION_ABSENT: "Required certification absent",
        FindingCode.CERTIFICATION_CONTRADICTED: "Certification contradicted by the record",
        FindingCode.IDENTIFYING_INFO_ABSENT: "Required identifying information absent",
        FindingCode.TIMING_INFO_ABSENT: "Required timing information absent",
        FindingCode.RATE_INFO_ABSENT: "Required rate information absent",
        FindingCode.DISPUTE_INFO_ABSENT: "Required dispute information absent",
        FindingCode.TIER_MISAPPLIED: "Tier table misapplied",
        FindingCode.FREE_TIME_WRONG: "Free time incorrectly applied",
        FindingCode.DOUBLE_BILLED: "Same day billed twice",
    }.get(code, code.value)


def _citations_for(evidence: list[Evidence], *kinds: EventKind) -> list[str]:
    wanted = set(kinds)
    return sorted({e.source_document_id for e in evidence if e.kind in wanted})


def evidence_completeness(
    evidence: list[Evidence], billed_days: list[object]
) -> float:
    """Share of billed days carrying a cited source document.

    Predicts dispute win rate better than any other single metric.
    """
    if not billed_days:
        return 1.0
    covered = {e.occurred_at.date() for e in evidence}
    hits = sum(1 for day in billed_days if day in covered)
    return hits / len(billed_days)
