"""The costed options table.

The Strategist does not produce a recommendation. It produces a menu with
expected values, and the workflow picks. That distinction matters: it keeps the
decision auditable and lets a spend cap veto the top choice without any
re-reasoning.

Priors come from the learning store (per depot, per terminal, per carrier). At
launch there are none, which is why the system should run in shadow mode first -
stated openly rather than papered over.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from agents.shared.models import Option, OptionsTable

# Costs of intervention. Real operational prices, mocked for the demo.
COST_EXPEDITE_DRAYAGE = Decimal("340")
COST_OFF_DOCK_YARD = Decimal("220")
COST_EXPEDITE_UNLOAD = Decimal("180")
COST_PREBOOK_RETURN = Decimal("0")
COST_FREE_TIME_EXTENSION = Decimal("0")

# Baseline priors, overridden per depot/terminal by the learning store.
PRIOR_EXPEDITE_SUCCESS = 0.72
PRIOR_OFF_DOCK_SUCCESS = 0.85
PRIOR_PREBOOK_SUCCESS = 0.90
PRIOR_EXTENSION_SUCCESS = 0.35
PRIOR_UNLOAD_SUCCESS = 0.95


def price_demurrage_options(
    container_id: str,
    *,
    as_of: datetime,
    exposure_usd: Decimal,
    intervention_suppressed: bool,
    suppression_reason: str = "",
    slot_scarcity_prior: float | None = None,
    auto_approve_limit_usd: Decimal = Decimal("250"),
) -> OptionsTable:
    """Options while the box is still in the terminal.

    When intervention is suppressed the table contains exactly one row: do
    nothing. That is the correct answer under a hold, and the counterfactual
    ledger later proves the agent was right not to spend.
    """
    do_nothing = Option(
        action="do_nothing",
        cost_usd=Decimal("0"),
        p_success=0.0,
        gross_saving_usd=Decimal("0"),
        note="baseline; charges continue to accrue",
    )

    if intervention_suppressed:
        return OptionsTable(
            container_id=container_id,
            assessed_at=as_of,
            options=[
                Option(
                    action="do_nothing",
                    cost_usd=Decimal("0"),
                    p_success=0.0,
                    gross_saving_usd=Decimal("0"),
                    note=suppression_reason or "intervention suppressed",
                )
            ],
        )

    expedite_p = PRIOR_EXPEDITE_SUCCESS
    if slot_scarcity_prior is not None:
        # Scarce slots cut the odds an expedited trucker achieves anything.
        expedite_p = max(0.05, PRIOR_EXPEDITE_SUCCESS * (1.0 - slot_scarcity_prior))

    options = [
        do_nothing,
        Option(
            action="expedite_drayage",
            cost_usd=COST_EXPEDITE_DRAYAGE,
            p_success=expedite_p,
            gross_saving_usd=exposure_usd,
            note="premium trucker with slot access",
            requires_approval=COST_EXPEDITE_DRAYAGE > auto_approve_limit_usd,
        ),
        Option(
            action="move_to_off_dock_yard",
            cost_usd=COST_OFF_DOCK_YARD,
            p_success=PRIOR_OFF_DOCK_SUCCESS,
            gross_saving_usd=exposure_usd,
            note="stops the terminal clock; adds a leg later",
            requires_approval=COST_OFF_DOCK_YARD > auto_approve_limit_usd,
        ),
        Option(
            action="request_free_time_extension",
            cost_usd=COST_FREE_TIME_EXTENSION,
            p_success=PRIOR_EXTENSION_SUCCESS,
            gross_saving_usd=exposure_usd,
            note="carrier-dependent; costs nothing to ask",
        ),
    ]

    return OptionsTable(container_id=container_id, assessed_at=as_of, options=options)


def price_detention_options(
    container_id: str,
    *,
    as_of: datetime,
    detention_exposure_usd: Decimal,
    depot_restriction_prior: float = 0.0,
    auto_approve_limit_usd: Decimal = Decimal("250"),
) -> OptionsTable:
    """Options once the box is out of the terminal.

    This is the only phase where the agent changes the outcome rather than
    documenting it, and the winning move usually costs nothing. Pre-booking the
    empty return is not a clever optimisation - it is the consequence of
    something still watching after everyone else stopped.

    ``depot_restriction_prior`` is the learning-store signal: a depot that has
    issued restrictions before makes slack expensive here.
    """
    prebook_p = min(0.99, PRIOR_PREBOOK_SUCCESS + (depot_restriction_prior * 0.05))

    options = [
        Option(
            action="do_nothing",
            cost_usd=Decimal("0"),
            p_success=0.0,
            gross_saving_usd=Decimal("0"),
            note="return the empty when convenient",
        ),
        Option(
            action="prebook_empty_return",
            cost_usd=COST_PREBOOK_RETURN,
            p_success=prebook_p,
            gross_saving_usd=detention_exposure_usd,
            note=(
                f"depot restriction prior {depot_restriction_prior:.2f}; "
                "buys slack while slack is still free"
            ),
        ),
        Option(
            action="expedite_unload",
            cost_usd=COST_EXPEDITE_UNLOAD,
            p_success=PRIOR_UNLOAD_SUCCESS,
            gross_saving_usd=detention_exposure_usd,
            note="frees the box sooner",
            requires_approval=COST_EXPEDITE_UNLOAD > auto_approve_limit_usd,
        ),
    ]

    return OptionsTable(container_id=container_id, assessed_at=as_of, options=options)
