"""Launch a ContainerWorkflow against a live Temporal dev server.

This is the missing live entrypoint: the tests drive the workflow with a
time-skipping environment, but nothing here starts one against a real server so
you can watch it in the Web UI.

Prerequisites (three terminals, from the repo root, venv active):

    1.  temporal server start-dev            # Web UI at http://localhost:8233
    2.  python -m temporal_backend.main      # the worker
    3.  python scripts/start_workflow.py      # this script

Usage:
    python scripts/start_workflow.py                 # start + drive the full signal sequence
    python scripts/start_workflow.py --fresh         # terminate any previous run first
    python scripts/start_workflow.py --no-drive      # just start; send signals yourself
    python scripts/start_workflow.py --step-seconds 5

Caveat on wall-clock timers: a live dev server does NOT skip time. The workflow
advances instantly through its ``wait_condition`` gates as each signal arrives,
but any internal polling ``sleep`` (availability polls, slot scans, LFD
checkpoints) runs in real time. For the compressed 45-day run, use the
time-skipping test: ``pytest temporal_backend/tests/test_lifecycle.py``.

The worked example is dated March 2026, which is in the past on a live server.
That is deliberate: it means every internal timer has already elapsed, so the
lifecycle plays out in seconds instead of waiting real days. The trade-off is
that an arc can close before the next scripted signal reaches it, so signals to
a finished arc are logged and skipped rather than aborting the run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from temporalio.client import WorkflowHandle  # noqa: E402
from temporalio.exceptions import WorkflowAlreadyStartedError  # noqa: E402
from temporalio.service import RPCError  # noqa: E402

from agents.shared.models import (  # noqa: E402
    ChargeType,
    ContainerInput,
    Hold,
    HoldType,
    Invoice,
    InvoiceLine,
)
from temporal_backend.shared.converter import TASK_QUEUE_IO, connect  # noqa: E402
from temporal_backend.workflows.container import (  # noqa: E402
    ContainerWorkflow,
    ContainerWorkflowInput,
)

log = logging.getLogger("pf.starter")

# The worked example from the runbook: MSKU 748192-0, Ningbo -> USLAX -> Fontana.
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

# A compact service contract for LLM_MODE=live term extraction. In mock/cache
# mode the Watcher fixture is used instead and this text is ignored.
SAMPLE_CONTRACT = """\
SERVICE CONTRACT SC-2026-0042 - PORT OF LOS ANGELES / LONG BEACH
SECTION 7. FREE TIME AND DEMURRAGE
7.1 Merchant is allowed four (4) calendar days of free time for import
    demurrage, counted from the day after discharge and availability.
7.2 After free time, demurrage accrues per container per day: days 1-3 at
    $200, days 4-6 at $325, day 7 onward at $450.
SECTION 8. DETENTION
8.1 Five (5) calendar days of free time on equipment after gate-out.
8.2 Detention accrues at $150/day for days 1-3, $225/day thereafter.
8.3 Chassis usage is billed at $45/day with no free time.
"""

# Timeline (mirrors the lifecycle test).
DISCHARGE = datetime(2026, 3, 3, 6, 0)
HOLD_PLACED = datetime(2026, 3, 4, 9, 30)
AVAILABLE = datetime(2026, 3, 5, 14, 0)
HOLD_RELEASED = datetime(2026, 3, 12, 16, 0)
GATE_OUT = datetime(2026, 3, 15, 14, 0)
STRIPPED = datetime(2026, 3, 17, 11, 0)
EMPTY_IN = datetime(2026, 3, 18, 9, 0)


def noncompliant_invoice() -> Invoice:
    """The demurrage invoice, missing its certification field (voids the charge)."""
    rates = [
        Decimal("200"), Decimal("200"), Decimal("200"),
        Decimal("325"), Decimal("325"), Decimal("325"),
        Decimal("450"), Decimal("450"),
    ]
    lines = [
        InvoiceLine(
            charge_type=ChargeType.DEMURRAGE,
            billed_day=i + 1,
            charge_date=(DISCHARGE.date() + timedelta(days=5 + i)),
            rate_usd=rate,
            amount_usd=rate,
        )
        for i, rate in enumerate(rates)
    ]
    return Invoice(
        invoice_id="INV-DEM-88431",
        charge_type=ChargeType.DEMURRAGE,
        billing_party="Maersk",
        container_id=CONTAINER.container_id,
        issued_at=datetime(2026, 4, 4).date(),
        last_charge_incurred_at=GATE_OUT.date(),
        total_usd=Decimal("2475"),
        lines=lines,
        free_time_claimed_days=4,
        lfd_claimed=datetime(2026, 3, 7).date(),
        certification_present=False,
        source_document_id="invoice::INV-DEM-88431",
    )


async def _wait_for(client, wf_id: str, timeout: float = 90.0, interval: float = 1.0) -> None:
    """Block until a workflow with ``wf_id`` exists on the server.

    Child arcs are spawned by the parent only after it finishes the (live, and
    therefore slow) contract-term extraction, so signalling a child on a fixed
    delay races the parent. Poll ``describe`` until the child is up instead.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            await client.get_workflow_handle(wf_id).describe()
            return
        except RPCError as exc:
            if "not found" not in str(exc).lower():
                raise
        if loop.time() > deadline:
            raise TimeoutError(
                f"{wf_id} did not appear within {timeout:.0f}s. Is the worker "
                f"running (Terminal 2: python -m temporal_backend.main)? In "
                f"LLM_MODE=live the parent's contract-term extraction can take "
                f"20-40s before the child spawns."
            )
        log.info("waiting for %s to spawn...", wf_id)
        await asyncio.sleep(interval)


CLOSED_MARKERS = ("already completed", "not found")


def _is_closed(exc: RPCError) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in CLOSED_MARKERS)


async def _signal(target, name, args: list, label: str) -> bool:
    """Signal a workflow, tolerating an arc that has already closed.

    The worked example is dated March 2026, but a live server runs on the real
    clock, so every window in that timeline has already elapsed. An arc can
    therefore finish before the next scripted signal reaches it. That is a
    property of the fixture dates rather than a failure, so note it and carry on
    - the invoice and dispute phase is the part worth watching.

    Returns:
        True if the signal was delivered, False if the target had already closed.
    """
    try:
        await target.signal(name, args=args)
        return True
    except RPCError as exc:
        if _is_closed(exc):
            log.warning("%s skipped: that arc has already closed", label)
            return False
        raise


async def _terminate_if_present(client, wf_id: str) -> None:
    """Terminate a workflow if it is still running; ignore it if it is not there."""
    try:
        await client.get_workflow_handle(wf_id).terminate(reason="--fresh restart")
        log.info("terminated %s", wf_id)
    except RPCError as exc:
        if not _is_closed(exc):
            raise


async def _print_state(handle: WorkflowHandle) -> None:
    try:
        state = await handle.query(ContainerWorkflow.state)
        log.info("container state: %s", state)
    except Exception as exc:  # noqa: BLE001 - query may fail if not yet started
        log.warning("could not query state: %s", exc)


async def drive(client, handle: WorkflowHandle, step: float) -> None:
    """Send the canonical lifecycle signal sequence with a delay between each."""
    cid = CONTAINER.container_id

    async def pause() -> None:
        await asyncio.sleep(step)

    log.info("D4  customs_entry_filed")
    await _signal(
        handle,
        ContainerWorkflow.customs_entry_filed,
        [DISCHARGE - timedelta(days=4), "ace::entry::SUM-4471"],
        "D4 customs_entry_filed",
    )
    await pause()

    log.info("D8  discharged  -> spawns DemurrageArc")
    await _signal(
        handle,
        ContainerWorkflow.discharged,
        [DISCHARGE, "edi315::VA::MSKU7481920"],
        "D8 discharged",
    )
    await pause()

    await _wait_for(client, f"demurrage::{cid}")
    dem = client.get_workflow_handle(f"demurrage::{cid}")

    log.info("D9  hold_placed (customs exam) -> intervention suppressed")
    await _signal(
        dem,
        "hold_placed",
        [Hold(hold_type=HoldType.CUSTOMS_EXAM, placed_at=HOLD_PLACED, reference="ace::CET-88213")],
        "D9 hold_placed",
    )
    await pause()

    log.info("D10 container_available (late) -> effective LFD shifts")
    await _signal(
        dem,
        "container_available",
        [AVAILABLE, "terminal::avail::MSKU7481920"],
        "D10 container_available",
    )
    await pause()

    log.info("D17 hold_released -> re-rank, resume action")
    await _signal(
        dem,
        "hold_released",
        [HoldType.CUSTOMS_EXAM, HOLD_RELEASED, "ace::CET-88213::rel"],
        "D17 hold_released",
    )
    await pause()

    log.info("D20 gate_out -> DemurrageArc closes, DetentionArc spawns")
    await _signal(
        dem, "gate_out", [GATE_OUT, "eir::gateout::MSKU7481920"], "D20 gate_out"
    )
    await pause()

    await _wait_for(client, f"detention::{cid}")
    det = client.get_workflow_handle(f"detention::{cid}")

    log.info("D22 cargo_stripped")
    await _signal(
        det, "cargo_stripped", [STRIPPED, "wms::strip::MSKU7481920"], "D22 cargo_stripped"
    )
    await pause()

    log.info("D23 empty_returned -> DetentionArc closes ($1,485 prevented)")
    await _signal(
        det, "empty_returned", [EMPTY_IN, "eir::MSKU7481920::D23"], "D23 empty_returned"
    )
    await pause()

    log.info("D40 invoice_received (non-compliant) -> spawns DisputeArc")
    filed = await _signal(
        handle,
        ContainerWorkflow.invoice_received,
        [noncompliant_invoice()],
        "D40 invoice_received",
    )
    if filed:
        try:
            await _wait_for(client, "dispute::INV-DEM-88431")
        except TimeoutError as exc:
            log.warning("%s", exc)
    await pause()

    await _print_state(handle)
    log.info(
        "Done driving. Watch dispute::INV-DEM-88431 in the UI; the parent stays "
        "alive on its invoice watch. Send `close` to finish it."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Start a ContainerWorkflow on the dev server")
    parser.add_argument("--no-drive", action="store_true", help="start only; do not send signals")
    parser.add_argument(
        "--step-seconds", type=float, default=3.0, help="delay between signals (default 3)"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="terminate any existing run for this container first, then start clean",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    client = await connect()

    if args.fresh:
        cid = CONTAINER.container_id
        for wf_id in (
            CONTAINER.workflow_id,
            f"demurrage::{cid}",
            f"detention::{cid}",
            "dispute::INV-DEM-88431",
        ):
            await _terminate_if_present(client, wf_id)

    try:
        handle = await client.start_workflow(
            ContainerWorkflow.run,
            ContainerWorkflowInput(
                container=CONTAINER,
                contract_text=SAMPLE_CONTRACT,
                auto_approve_limit_usd=Decimal("250"),
                slot_scarcity_prior=0.8,
                depot_restriction_prior=0.6,
            ),
            id=CONTAINER.workflow_id,
            task_queue=TASK_QUEUE_IO,
        )
        log.info("started workflow id=%s", CONTAINER.workflow_id)
    except WorkflowAlreadyStartedError:
        log.warning(
            "workflow %s is already running; reusing it. Its arcs may already have "
            "closed, in which case their signals are skipped. Re-run with --fresh "
            "for a clean lifecycle.",
            CONTAINER.workflow_id,
        )
        handle = client.get_workflow_handle(CONTAINER.workflow_id)
    log.info("view it at http://localhost:8233/namespaces/default/workflows/%s", CONTAINER.workflow_id)

    if args.no_drive:
        log.info("--no-drive set; not sending signals. Workflow is parked awaiting discharge.")
        return

    await drive(client, handle, args.step_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
