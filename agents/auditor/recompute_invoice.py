"""Recompute the invoice from first principles.

The billing party applies its tier table to the nominal LFD. We apply the
contract's tier table to the *effective* LFD. The difference is claimable, and
because it is arithmetic it is not arguable.

Three defects show up routinely:
  * free time understated relative to the contract
  * tier bands applied one step too aggressively
  * weekends counted when the contract excludes them
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from agents.shared.charges import charge_for_period, last_free_day
from agents.shared.models import (
    ChargeType,
    ClockTerms,
    ContractTerms,
    Finding,
    FindingCode,
    Invoice,
)


def recompute(
    invoice: Invoice,
    terms: ContractTerms,
    *,
    clock_start: date,
    clock_end: date,
    effective_lfd: date | None = None,
    holidays: frozenset[date] | None = None,
) -> tuple[Decimal, list[Finding]]:
    """Recompute the charge and report every discrepancy found.

    Args:
        invoice: The invoice as received.
        terms: Terms extracted from the contract.
        clock_start: Discharge (demurrage/storage) or gate-out (detention).
        clock_end: The day the clock actually stopped.
        effective_lfd: If supplied and later than the contractual LFD, the charge
            is recomputed from it, which is how leak 01 turns into money.

    Returns:
        ``(recomputed_total, findings)``
    """
    if invoice.charge_type not in terms.clocks:
        return invoice.total_usd, []

    clock = terms.clock(invoice.charge_type)
    findings: list[Finding] = []

    findings.extend(_check_free_time(invoice, clock))

    contractual_lfd = last_free_day(clock, clock_start)
    findings.extend(_check_claimed_lfd(invoice, contractual_lfd))

    effective_start = clock_start
    if effective_lfd and effective_lfd > contractual_lfd:
        # Charge as though free time began when the box was genuinely available.
        shift = (effective_lfd - contractual_lfd).days
        effective_start = clock_start + _days(shift)

    recomputed, breakdown = charge_for_period(
        clock, effective_start, clock_end, holidays=holidays
    )

    if recomputed < invoice.total_usd:
        delta = invoice.total_usd - recomputed
        findings.append(
            Finding(
                code=FindingCode.TIER_MISAPPLIED,
                basis="contract tier table",
                detail=(
                    f"recomputed {invoice.charge_type.value} over "
                    f"{len(breakdown)} billable day(s) from the contract tier "
                    f"table is ${recomputed}, against ${invoice.total_usd} billed"
                ),
                recompute_delta_usd=delta,
            )
        )

    if not clock.counts_weekends:
        findings.extend(_check_weekend_counting(invoice, clock))

    return recomputed, findings


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def _check_free_time(invoice: Invoice, clock: ClockTerms) -> list[Finding]:
    if invoice.free_time_claimed_days is None:
        return []
    if invoice.free_time_claimed_days >= clock.free_days:
        return []
    return [
        Finding(
            code=FindingCode.FREE_TIME_WRONG,
            basis="service contract",
            detail=(
                f"invoice applies {invoice.free_time_claimed_days} free days; the "
                f"contract grants {clock.free_days} at this port"
            ),
        )
    ]


def _check_claimed_lfd(invoice: Invoice, contractual_lfd: date) -> list[Finding]:
    if invoice.lfd_claimed is None or invoice.lfd_claimed >= contractual_lfd:
        return []
    return [
        Finding(
            code=FindingCode.FREE_TIME_WRONG,
            basis="service contract",
            detail=(
                f"invoice asserts a last free day of "
                f"{invoice.lfd_claimed.isoformat()}; the contract yields "
                f"{contractual_lfd.isoformat()}"
            ),
        )
    ]


def _check_weekend_counting(invoice: Invoice, clock: ClockTerms) -> list[Finding]:
    weekend_lines = [ln for ln in invoice.lines if ln.charge_date.weekday() >= 5]
    if not weekend_lines:
        return []
    amount = sum((ln.amount_usd for ln in weekend_lines), Decimal("0"))
    days = ", ".join(ln.charge_date.isoformat() for ln in weekend_lines[:5])
    return [
        Finding(
            code=FindingCode.TIER_MISAPPLIED,
            basis="contract weekend counting rule",
            detail=(
                f"{len(weekend_lines)} weekend day(s) billed (${amount}) although "
                f"the contract excludes weekends: {days}"
            ),
            recompute_delta_usd=amount,
        )
    ]
