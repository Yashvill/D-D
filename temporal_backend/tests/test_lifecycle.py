"""The 45-day lifecycle, run in about a second of wall clock.

These are the five assertions from the runbook that carry the entire argument:

    D9   spend == $0 while a hold is open      the agent does not burn money to look busy
    D10  effective_lfd == D14, not D12          leak 01 is detected, not assumed away
    D18  appointment_failures >= 3              evidence is produced by trying, not remembering
    D21  return_slot booked before D24          the $1,485 prevention actually fires
    D40  Part 541 defect detected               the audit finds what a human skims past

Run:  pytest temporal_backend/tests/test_lifecycle.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

os.environ.setdefault("LLM_MODE", "mock")

from agents.shared.models import (  # noqa: E402
    ChargeType,
    ClockTerms,
    ContainerInput,
    ContractTerms,
    Hold,
    HoldType,
    Invoice,
    InvoiceLine,
    Tier,
)
from temporal_backend.activities.registry import ALL_ACTIVITIES  # noqa: E402
from temporal_backend.shared.converter import DATA_CONVERTER  # noqa: E402
from temporal_backend.workflows.container import (  # noqa: E402
    ContainerWorkflow,
    ContainerWorkflowInput,
)
from temporal_backend.workflows.demurrage import DemurrageArc  # noqa: E402
from temporal_backend.workflows.detention import DetentionArc  # noqa: E402
from temporal_backend.workflows.dispute import DisputeArc  # noqa: E402

TASK_QUEUE = "pf-test"

# The worked example: MSKU 748192-0, Ningbo -> USLAX -> Fontana, March 2026.
CONTAINER = ContainerInput(
    container_id="MSKU7481920",
    contract_id="SC-2026-0042",
    port="USLAX",
    terminal="Pier400",
    carrier="Maersk",
    consignee="Acme Garden Retail",
    return_depot="Fontana Empty Depot",
    bill_of_lading="MAEU123456789",
)

D1 = datetime(2026, 2, 24, 8, 0)
DISCHARGE = datetime(2026, 3, 3, 6, 0)
AVAILABLE = datetime(2026, 3, 5, 14, 0)
HOLD_PLACED = datetime(2026, 3, 4, 9, 30)
HOLD_RELEASED = datetime(2026, 3, 12, 16, 0)
GATE_OUT = datetime(2026, 3, 15, 14, 0)
STRIPPED = datetime(2026, 3, 17, 11, 0)
EMPTY_IN = datetime(2026, 3, 18, 9, 0)


def demo_terms() -> ContractTerms:
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
            ),
            ChargeType.DETENTION: ClockTerms(
                charge_type=ChargeType.DETENTION,
                billed_by="Carrier",
                free_days=5,
                tiers=[
                    Tier(from_day=1, to_day=3, rate_usd=Decimal("150")),
                    Tier(from_day=4, to_day=None, rate_usd=Decimal("225")),
                ],
            ),
            ChargeType.CHASSIS: ClockTerms(
                charge_type=ChargeType.CHASSIS,
                billed_by="Chassis pool",
                free_days=0,
                tiers=[Tier(from_day=1, to_day=None, rate_usd=Decimal("45"))],
            ),
        },
    )


def noncompliant_invoice() -> Invoice:
    """The demurrage invoice, missing its certification field.

    Under 541.5 that omission eliminates the obligation to pay the charge - worth
    the entire $2,475, and it takes the Auditor a few milliseconds to find.
    """
    lines = [
        InvoiceLine(
            charge_type=ChargeType.DEMURRAGE,
            billed_day=i + 1,
            charge_date=(DISCHARGE.date() + timedelta(days=5 + i)),
            rate_usd=rate,
            amount_usd=rate,
        )
        for i, rate in enumerate(
            [
                Decimal("200"),
                Decimal("200"),
                Decimal("200"),
                Decimal("325"),
                Decimal("325"),
                Decimal("325"),
                Decimal("450"),
                Decimal("450"),
            ]
        )
    ]
    return Invoice(
        invoice_id="INV-DEM-88431",
        charge_type=ChargeType.DEMURRAGE,
        billing_party="Maersk",
        container_id="MSKU7481920",
        issued_at=datetime(2026, 4, 4).date(),
        last_charge_incurred_at=GATE_OUT.date(),
        total_usd=Decimal("2475"),
        lines=lines,
        free_time_claimed_days=4,
        lfd_claimed=datetime(2026, 3, 7).date(),
        certification_present=False,
        source_document_id="invoice::INV-DEM-88431",
    )


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=DATA_CONVERTER
    ) as e:
        yield e


@pytest.fixture
async def worker(env):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ContainerWorkflow, DemurrageArc, DetentionArc, DisputeArc],
        activities=ALL_ACTIVITIES,
    ) as w:
        yield w


async def _start(env):
    return await env.client.start_workflow(
        ContainerWorkflow.run,
        ContainerWorkflowInput(
            container=CONTAINER,
            contract_text="mocked in LLM_MODE=mock",
            auto_approve_limit_usd=Decimal("250"),
            slot_scarcity_prior=0.8,
            depot_restriction_prior=0.6,
        ),
        id=CONTAINER.workflow_id,
        task_queue=TASK_QUEUE,
    )


@pytest.mark.asyncio
async def test_terms_loaded_and_agent_sleeps_before_discharge(env, worker):
    """Days 1-7: the agent exists before the vessel does, and costs nothing."""
    handle = await _start(env)

    await env.sleep(timedelta(hours=2))
    state = await handle.query(ContainerWorkflow.state)

    assert state["terms_loaded"] is True
    assert state["terms_confidence"] == pytest.approx(0.91)
    # Still parked waiting for discharge.
    assert state["discharged_at"] is None

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_demurrage_arc_suppresses_spend_and_shifts_lfd(env, worker):
    """Assertions D9 and D10, the two that matter most on the terminal side."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))

    await handle.signal(
        ContainerWorkflow.customs_entry_filed,
        args=[D1 + timedelta(days=3), "ace::entry::SUM-4471"],
    )
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )

    # Let the demurrage child come up.
    await env.sleep(timedelta(hours=2))
    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")

    # D9: hold placed. The box is legally immovable.
    await dem.signal(
        DemurrageArc.hold_placed,
        Hold(
            hold_type=HoldType.CUSTOMS_EXAM,
            placed_at=HOLD_PLACED,
            reference="ace::CET-88213",
        ),
    )
    # D10: availability lands late, two free days already gone.
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )

    await env.sleep(timedelta(days=8))
    state = await dem.query(DemurrageArc.state)

    # --- assertion D10: leak 01 detected, not assumed away
    assert state["nominal_lfd"] == "2026-03-07"
    assert state["effective_lfd"] == "2026-03-09"
    assert state["lfd_shifted"] is True

    # --- assertion D9: the anti-waste guarantee
    assert Decimal(state["spend_usd"]) == Decimal("0")
    assert state["intervention_suppressed"] is True
    assert "customs_exam" in state["holds"]
    assert state["risk"] == "RED"

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_appointment_failures_are_recorded_as_evidence(env, worker):
    """Assertion D18: evidence is produced by trying, not by remembering."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )
    await env.sleep(timedelta(hours=2))

    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )

    # No hold, so the agent is free to scan. Every miss becomes a record.
    await env.sleep(timedelta(days=6))
    state = await dem.query(DemurrageArc.state)

    assert state["appointment_failures"] >= 3, state
    assert state["evidence_count"] >= 3

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_empty_return_booked_before_restriction_bites(env, worker):
    """Assertion D21: the $1,485 prevention actually fires."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )
    await env.sleep(timedelta(hours=2))

    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )
    await env.sleep(timedelta(days=2))
    await dem.signal(
        DemurrageArc.gate_out, args=[GATE_OUT, "eir::gateout::MSKU7481920"]
    )

    # Detention arc spawns and prices the second clock a day later.
    await env.sleep(timedelta(days=2))
    det = env.client.get_workflow_handle(f"detention::{CONTAINER.container_id}")
    state = await det.query(DetentionArc.state)

    # --- assertion D21
    assert state["return_slot"] is not None, state
    booked = datetime.fromisoformat(state["return_slot"])
    # Free time expires D25 (Mar 20); the restriction starts Mar 19.
    assert booked.date() < datetime(2026, 3, 19).date(), f"booked {booked} too late"
    assert Decimal(state["prevented_usd"]) > Decimal("0")

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_restriction_after_return_is_a_near_miss(env, worker):
    """D24: the agent did not predict the restriction; it bought free slack."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )
    await env.sleep(timedelta(hours=2))

    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )
    await env.sleep(timedelta(days=2))
    await dem.signal(
        DemurrageArc.gate_out, args=[GATE_OUT, "eir::gateout::MSKU7481920"]
    )
    await env.sleep(timedelta(days=2))

    det = env.client.get_workflow_handle(f"detention::{CONTAINER.container_id}")
    await det.signal(
        DetentionArc.cargo_stripped, args=[STRIPPED, "wms::strip::MSKU7481920"]
    )
    await det.signal(
        DetentionArc.empty_returned, args=[EMPTY_IN, "eir::MSKU7481920::D23"]
    )

    # The advisory lands the day after the box is already back.
    await det.signal(
        DetentionArc.carrier_advisory,
        args=[
            "Effective 19 March, empties will not be accepted at Fontana Empty Depot.",
            "advisory::MAEU-2026-0311",
            datetime(2026, 3, 19, 7, 0),
        ],
    )

    await env.sleep(timedelta(days=3))
    state = await det.query(DetentionArc.state)

    assert state["empty_returned_at"] is not None
    assert state["detention_days"] == 0, "detention should never have accrued"
    assert state["near_miss"] is True, state

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_part_541_defect_detected_and_dispute_filed(env, worker):
    """Assertion D40: the audit finds what a human skims past."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )
    await env.sleep(timedelta(hours=2))

    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")
    await dem.signal(
        DemurrageArc.hold_placed,
        Hold(hold_type=HoldType.CUSTOMS_EXAM, placed_at=HOLD_PLACED, reference="ace::CET-88213"),
    )
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )
    await env.sleep(timedelta(days=1))
    await dem.signal(
        DemurrageArc.hold_released,
        args=[HoldType.CUSTOMS_EXAM, HOLD_RELEASED, "ace::CET-88213::rel"],
    )
    await env.sleep(timedelta(days=2))
    await dem.signal(
        DemurrageArc.gate_out, args=[GATE_OUT, "eir::gateout::MSKU7481920"]
    )
    await env.sleep(timedelta(days=2))

    det = env.client.get_workflow_handle(f"detention::{CONTAINER.container_id}")
    await det.signal(
        DetentionArc.cargo_stripped, args=[STRIPPED, "wms::strip::MSKU7481920"]
    )
    await det.signal(
        DetentionArc.empty_returned, args=[EMPTY_IN, "eir::MSKU7481920::D23"]
    )
    await env.sleep(timedelta(days=3))

    # D40: the invoice arrives, missing its certification.
    await handle.signal(ContainerWorkflow.invoice_received, noncompliant_invoice())
    await env.sleep(timedelta(days=2))

    dispute = env.client.get_workflow_handle("dispute::INV-DEM-88431")
    findings = await dispute.query(DisputeArc.findings)

    # --- assertion D40
    assert "certification_absent" in findings, findings

    dstate = await dispute.query(DisputeArc.state)
    assert dstate["voids_entire_charge"] is True
    assert dstate["filed"] is True
    assert dstate["claims"] >= 1
    # Never contest more than was billed.
    assert Decimal(dstate["amount_contested_usd"]) <= Decimal("2475")

    await handle.signal(ContainerWorkflow.close)


@pytest.mark.asyncio
async def test_evidence_log_is_fully_cited(env, worker):
    """Every recorded fact carries a source_document_id, or it is not there."""
    handle = await _start(env)
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        ContainerWorkflow.discharged, args=[DISCHARGE, "edi315::VA::MSKU7481920"]
    )
    await env.sleep(timedelta(hours=2))

    dem = env.client.get_workflow_handle(f"demurrage::{CONTAINER.container_id}")
    await dem.signal(
        DemurrageArc.container_available,
        args=[AVAILABLE, "terminal::avail::MSKU7481920"],
    )
    await env.sleep(timedelta(days=2))
    await dem.signal(
        DemurrageArc.gate_out, args=[GATE_OUT, "eir::gateout::MSKU7481920"]
    )
    await env.sleep(timedelta(days=2))

    log = await handle.query(ContainerWorkflow.evidence_log)
    assert log, "evidence log should not be empty"
    for entry in log:
        assert entry["source_document_id"].strip(), entry

    await handle.signal(ContainerWorkflow.close)
