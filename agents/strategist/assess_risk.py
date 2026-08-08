"""Risk assessment and the counterfactual ledger.

Rule-based on purpose. The inputs are known dates and a published tier table, so
the output is arithmetic. Making this deterministic means the demo cannot
embarrass you, and the counterfactual is auditable rather than asserted.

The important behaviour here is *suppression*: while a container is under a
customs hold it is legally immovable, and spending $340 on an expedited trucker
achieves nothing. A naive agent fires every countermeasure it has. This one
records the hold and does nothing, then proves later that doing nothing was
correct.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from agents.shared.charges import charge_for_period, marginal_cost_of_day
from agents.shared.models import (
    ChargeType,
    ContractTerms,
    Hold,
    RiskAssessment,
    RiskLevel,
)

# Days-to-LFD thresholds for the traffic light.
RED_THRESHOLD_DAYS = 1
YELLOW_THRESHOLD_DAYS = 3


def assess(
    container_id: str,
    terms: ContractTerms,
    *,
    as_of: datetime,
    effective_lfd: date,
    clock_start: date,
    holds: list[Hold],
    projected_end: date,
    gated_out: bool = False,
) -> RiskAssessment:
    """Score current exposure and store the counterfactual.

    ``counterfactual_usd`` is what we predict gets billed if we do nothing. It
    is checked against reality later, which is what turns a savings claim into
    evidence.
    """
    today = as_of.date()
    open_holds = [h for h in holds if h.is_open]

    demurrage = terms.clock(ChargeType.DEMURRAGE)
    counterfactual, _ = charge_for_period(demurrage, clock_start, projected_end)

    days_to_lfd = (effective_lfd - today).days

    if gated_out:
        return RiskAssessment(
            container_id=container_id,
            assessed_at=as_of,
            level=RiskLevel.CLOSED,
            reason="gated out; demurrage clock stopped",
            counterfactual_usd=counterfactual,
            days_to_lfd=days_to_lfd,
        )

    # A hold means the box is legally immovable. Suppress spending entirely -
    # this is the anti-waste guarantee, and it is asserted in the test suite.
    if open_holds:
        kinds = ", ".join(sorted(h.hold_type.value for h in open_holds))
        return RiskAssessment(
            container_id=container_id,
            assessed_at=as_of,
            level=RiskLevel.RED,
            reason=f"blocked: {kinds}",
            counterfactual_usd=counterfactual,
            days_to_lfd=days_to_lfd,
            intervention_suppressed=True,
            suppression_reason=(
                "container is legally immovable under an open hold; "
                "no intervention can change the outcome"
            ),
        )

    if days_to_lfd <= RED_THRESHOLD_DAYS:
        marginal = marginal_cost_of_day(demurrage, clock_start, max(today, effective_lfd))
        return RiskAssessment(
            container_id=container_id,
            assessed_at=as_of,
            level=RiskLevel.RED,
            reason=f"LFD in {days_to_lfd}d; next day costs ${marginal}",
            counterfactual_usd=counterfactual,
            days_to_lfd=days_to_lfd,
        )

    if days_to_lfd <= YELLOW_THRESHOLD_DAYS:
        return RiskAssessment(
            container_id=container_id,
            assessed_at=as_of,
            level=RiskLevel.YELLOW,
            reason=f"LFD in {days_to_lfd}d; action still cheap",
            counterfactual_usd=counterfactual,
            days_to_lfd=days_to_lfd,
        )

    return RiskAssessment(
        container_id=container_id,
        assessed_at=as_of,
        level=RiskLevel.GREEN,
        reason=f"LFD in {days_to_lfd}d",
        counterfactual_usd=counterfactual,
        days_to_lfd=days_to_lfd,
    )


def detention_exposure(
    terms: ContractTerms,
    gated_out_at: date,
    projected_return: date,
    *,
    chassis_start: date | None = None,
) -> Decimal:
    """Detention plus chassis exposure if the empty goes back on ``projected_return``.

    This is the number that justifies the single highest-value action in the
    system, and it is why the second clock must be watched after everyone else
    has filed the container as done.

    The two clocks do not share a start. Detention runs from gate-out. Chassis
    per diem accrues on the frame while the *empty* sits waiting, so it starts
    after the cargo is stripped - the day the box becomes a parked empty rather
    than a delivery in progress. Conflating them overstates exposure, which
    would make the agent look like it is inflating its own savings.

    Args:
        chassis_start: Day the chassis clock begins. Defaults to ``gated_out_at``
            when the stripping date is not yet known.
    """
    total = Decimal("0")

    if ChargeType.DETENTION in terms.clocks:
        amount, _ = charge_for_period(
            terms.clock(ChargeType.DETENTION), gated_out_at, projected_return
        )
        total += amount

    if ChargeType.CHASSIS in terms.clocks:
        amount, _ = charge_for_period(
            terms.clock(ChargeType.CHASSIS),
            chassis_start or gated_out_at,
            projected_return,
        )
        total += amount

    return total
