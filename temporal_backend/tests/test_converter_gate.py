"""Phase 0 gate: prove Pydantic models survive the Temporal boundary.

If this fails, nothing else in the project can work. The default payload
converter cannot serialise Pydantic models, and ``date``/``datetime`` fields do
not round-trip at all without ``pydantic_data_converter``.

Run first:  pytest temporal_backend/tests/test_converter_gate.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agents.shared.models import (
    ChargeType,
    ClockTerms,
    ContractTerms,
    Evidence,
    EventKind,
    LfdCalculation,
    Tier,
)
from temporal_backend.shared.converter import DATA_CONVERTER


@activity.defn
async def echo_terms(terms: ContractTerms) -> ContractTerms:
    return terms


@activity.defn
async def echo_lfd(calc: LfdCalculation) -> LfdCalculation:
    return calc


@activity.defn
async def echo_evidence(item: Evidence) -> Evidence:
    return item


@workflow.defn
class ConverterGateWorkflow:
    @workflow.run
    async def run(self, terms: ContractTerms, calc: LfdCalculation, ev: Evidence) -> dict:
        opts = {"start_to_close_timeout": timedelta(seconds=10)}

        back_terms = await workflow.execute_activity(echo_terms, terms, **opts)
        back_calc = await workflow.execute_activity(echo_lfd, calc, **opts)
        back_ev = await workflow.execute_activity(echo_evidence, ev, **opts)

        return {
            "free_days": back_terms.clock(ChargeType.DEMURRAGE).free_days,
            "tier_day8_rate": str(back_terms.clock(ChargeType.DEMURRAGE).rate_for(8)),
            "confidence": back_terms.confidence,
            "nominal_lfd": back_calc.nominal_lfd.isoformat(),
            "effective_lfd": back_calc.effective_lfd.isoformat(),
            "shifted": back_calc.shifted,
            "ungrounded_days": back_calc.ungrounded_days,
            "evidence_doc_id": back_ev.source_document_id,
            "evidence_kind": back_ev.kind.value,
        }


def _terms() -> ContractTerms:
    return ContractTerms(
        contract_id="SC-2026-0042",
        port="USLAX",
        carrier="Maersk",
        confidence=0.91,
        source_document_id="contract::SC-2026-0042",
        clocks={
            ChargeType.DEMURRAGE: ClockTerms(
                charge_type=ChargeType.DEMURRAGE,
                billed_by="Carrier",
                free_days=4,
                tiers=[
                    Tier(from_day=1, to_day=3, rate_usd=Decimal("200")),
                    Tier(from_day=4, to_day=6, rate_usd=Decimal("325")),
                    Tier(from_day=7, to_day=None, rate_usd=Decimal("450")),
                ],
            )
        },
    )


@pytest.mark.asyncio
async def test_pydantic_models_round_trip_through_temporal():
    """The gate. Pydantic models, Decimals, dates and enums must all survive."""
    terms = _terms()
    calc = LfdCalculation(
        nominal_lfd=date(2026, 3, 7),
        effective_lfd=date(2026, 3, 9),
        ungrounded_days=2,
        discharged_at=datetime(2026, 3, 3, 6, 0),
        available_at=datetime(2026, 3, 5, 14, 0),
        citations=["terminal::availability::MSKU7481920"],
    )
    evidence = Evidence(
        kind=EventKind.HOLD_PLACED,
        occurred_at=datetime(2026, 3, 4, 9, 30),
        source_system="ACE",
        source_document_id="ace::CET-88213",
        summary="CET exam hold placed",
    )

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue="gate-queue",
            workflows=[ConverterGateWorkflow],
            activities=[echo_terms, echo_lfd, echo_evidence],
        ):
            result = await env.client.execute_workflow(
                ConverterGateWorkflow.run,
                args=[terms, calc, evidence],
                id="converter-gate",
                task_queue="gate-queue",
            )

    # Pydantic model + dict-keyed-by-enum + nested list survived
    assert result["free_days"] == 4
    # Decimal survived without becoming a float
    assert result["tier_day8_rate"] == "450"
    assert result["confidence"] == pytest.approx(0.91)
    # date fields survived - these fail entirely without the converter
    assert result["nominal_lfd"] == "2026-03-07"
    assert result["effective_lfd"] == "2026-03-09"
    assert result["shifted"] is True
    assert result["ungrounded_days"] == 2
    # citation provenance survived
    assert result["evidence_doc_id"] == "ace::CET-88213"
    assert result["evidence_kind"] == "hold_placed"


def test_evidence_rejects_missing_citation():
    """No citation, no claim - enforced by the model, not by a prompt."""
    with pytest.raises(ValueError):
        Evidence(
            kind=EventKind.APPOINTMENT_UNAVAILABLE,
            occurred_at=datetime(2026, 3, 13, 8, 0),
            source_system="terminal",
            source_document_id="   ",
            summary="no slots",
        )
