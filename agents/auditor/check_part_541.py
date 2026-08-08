"""The 46 CFR Part 541 invoice checklist.

The most legally grounded agent of the five, and entirely rule-based. This is
the cheapest money in the system: a meaningful share of charges are simply wrong
or non-compliant, independent of whether the underlying delay was justified.

Two provisions do the work:
  * 541.7(a) - a billing party must issue within 30 days of the date the charge
    was last incurred. Miss it and the billed party has no obligation to pay.
  * 541.5 / 541.6 - omitting any applicable required content item eliminates the
    obligation to pay that charge.

The certification clause is the sharpest lever. Every compliant US D&D invoice
carries the billing party's written statement that its own performance did not
cause or contribute to the charge. When our recorded evidence shows the terminal
had no appointment slots, that is not a sympathy argument - it is a documented
contradiction of a signed certification.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from agents.shared.models import (
    AuditResult,
    Evidence,
    EventKind,
    Finding,
    FindingCode,
    Invoice,
)

ISSUANCE_WINDOW_DAYS = 30

# Evidence kinds that contradict a "our performance did not contribute"
# certification, mapped to the reason they do.
CONTRADICTING_EVIDENCE: dict[EventKind, str] = {
    EventKind.APPOINTMENT_UNAVAILABLE: (
        "the terminal's own appointment system had no slots on days billed at "
        "the top tier"
    ),
    EventKind.AVAILABILITY_MISS: (
        "the container was not grounded or appointable during the billed window"
    ),
    EventKind.CARRIER_ADVISORY: (
        "the carrier declined to accept empties at the designated depot during "
        "the billed window"
    ),
}


def check_issuance_deadline(invoice: Invoice) -> Finding | None:
    """541.7(a): issued within 30 days of the last incurred charge."""
    age = (invoice.issued_at - invoice.last_charge_incurred_at).days
    if age <= ISSUANCE_WINDOW_DAYS:
        return None
    return Finding(
        code=FindingCode.LATE_ISSUANCE,
        basis="46 CFR 541.7(a)",
        detail=(
            f"invoice issued {age} days after the charge was last incurred on "
            f"{invoice.last_charge_incurred_at.isoformat()}, exceeding the "
            f"{ISSUANCE_WINDOW_DAYS}-day statutory window"
        ),
        voids_charge=True,
    )


def check_required_content(invoice: Invoice) -> list[Finding]:
    """541.6 content categories; 541.5 makes any omission fatal to the charge."""
    checks = [
        (invoice.identifying_info_present, FindingCode.IDENTIFYING_INFO_ABSENT, "identifying"),
        (invoice.timing_info_present, FindingCode.TIMING_INFO_ABSENT, "timing"),
        (invoice.rate_info_present, FindingCode.RATE_INFO_ABSENT, "rate"),
        (invoice.dispute_info_present, FindingCode.DISPUTE_INFO_ABSENT, "dispute"),
        (invoice.certification_present, FindingCode.CERTIFICATION_ABSENT, "certification"),
    ]

    findings: list[Finding] = []
    for present, code, label in checks:
        if present:
            continue
        findings.append(
            Finding(
                code=code,
                basis="46 CFR 541.6, 541.5",
                detail=(
                    f"required {label} information is absent; under 541.5 this "
                    f"eliminates the obligation to pay this charge"
                ),
                voids_charge=True,
            )
        )
    return findings


def check_certification_contradicted(
    invoice: Invoice, evidence: list[Evidence]
) -> Finding | None:
    """The strongest available lever when the certification IS present.

    A present certification asserts the billing party did not contribute to the
    charge. Our contemporaneous record may say otherwise.
    """
    if not invoice.certification_present:
        return None  # already voided outright by check_required_content

    grounds: list[str] = []
    citations: list[str] = []

    for item in evidence:
        reason = CONTRADICTING_EVIDENCE.get(item.kind)
        if reason and reason not in grounds:
            grounds.append(reason)
        if reason:
            citations.append(item.source_document_id)

    if not grounds:
        return None

    return Finding(
        code=FindingCode.CERTIFICATION_CONTRADICTED,
        basis="46 CFR 541.6 certification clause",
        detail=(
            "the invoice certifies that the billing party's performance did not "
            "cause or contribute to the charge, which the contemporaneous record "
            "contradicts: "
            + "; ".join(grounds)
            + ". Cited: "
            + ", ".join(sorted(set(citations))[:6])
        ),
        voids_charge=False,
    )


def check_double_billing(invoices: list[Invoice]) -> list[Finding]:
    """The same day billed twice under two labels.

    Two invoices for overlapping days from two companies is normal under
    merchant haulage, which is exactly why genuine double-billing hides inside a
    legitimate-looking overlap.
    """
    findings: list[Finding] = []
    seen: dict[tuple[str, date], list[str]] = {}

    for invoice in invoices:
        for line in invoice.lines:
            key = (invoice.charge_type.value, line.charge_date)
            seen.setdefault(key, []).append(invoice.invoice_id)

    for (charge_type, day), ids in seen.items():
        if len(ids) > 1:
            findings.append(
                Finding(
                    code=FindingCode.DOUBLE_BILLED,
                    basis="contract / tariff",
                    detail=(
                        f"{charge_type} billed more than once for "
                        f"{day.isoformat()} across invoices {', '.join(sorted(set(ids)))}"
                    ),
                    voids_charge=False,
                )
            )

    return findings


def audit(
    invoice: Invoice,
    evidence: list[Evidence],
    *,
    audited_at: datetime,
    recomputed_total_usd: Decimal | None = None,
    extra_findings: list[Finding] | None = None,
) -> AuditResult:
    """Run the full checklist against one invoice."""
    findings: list[Finding] = []

    deadline = check_issuance_deadline(invoice)
    if deadline:
        findings.append(deadline)

    findings.extend(check_required_content(invoice))

    contradiction = check_certification_contradicted(invoice, evidence)
    if contradiction:
        findings.append(contradiction)

    findings.extend(extra_findings or [])

    return AuditResult(
        invoice_id=invoice.invoice_id,
        audited_at=audited_at,
        findings=findings,
        billed_total_usd=invoice.total_usd,
        recomputed_total_usd=(
            recomputed_total_usd if recomputed_total_usd is not None else invoice.total_usd
        ),
    )
