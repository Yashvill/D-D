"""Unit tests for api.py's history-based approval visibility.

The risk in _pending_approval isn't FastAPI routing, it's whether decoding
Temporal history correctly answers "does a matching approve signal already
exist" - the exact question DemurrageArc/DetentionArc ask themselves via
``choice.action in self.approvals``. Exercised directly against a
time-skipping environment rather than over HTTP.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

os.environ.setdefault("LLM_MODE", "mock")

import temporal_backend.api as api_module  # noqa: E402
from agents.shared.models import (  # noqa: E402
    ChargeType,
    ClockTerms,
    ContainerInput,
    ContractTerms,
    Tier,
)
from temporal_backend.activities.registry import ALL_ACTIVITIES  # noqa: E402
from temporal_backend.shared.converter import DATA_CONVERTER  # noqa: E402
from temporal_backend.workflows.demurrage import DemurrageArc, DemurrageInput  # noqa: E402

TASK_QUEUE = "pf-api-approvals-test"

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
        },
    )


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping(data_converter=DATA_CONVERTER) as e:
        # api.py reads the module-level _client rather than connecting itself,
        # so pointing it at the time-skipping environment's client is enough to
        # exercise the real endpoint functions with no server/worker running.
        api_module._client = e.client
        yield e
        api_module._client = None


@pytest.fixture
async def worker(env):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DemurrageArc],
        activities=ALL_ACTIVITIES,
    ) as w:
        yield w


@pytest.mark.asyncio
async def test_pending_approval_reflects_history_and_clears_on_approve(env, worker):
    """No hold, a spend cap low enough that every actionable option needs a
    human, and a live run past the first pre-LFD checkpoint: the arc parks on
    notify_human, the endpoint surfaces exactly that action/cost/reason, and
    approving it - the same signal the console's Approve button sends - makes
    the pending approval disappear."""
    # Discharge is anchored to the workflow's own "now" rather than the fixed
    # March-2026 worked example used elsewhere: the pre-LFD checkpoints compare
    # their target against workflow.now(), and a historical discharge date puts
    # every one of those targets in the past relative to a time-skipping
    # environment's real-time clock, so the loop skips straight to phase 3
    # (slot scanning) and notify_human is never reached by this path.
    discharge = datetime.utcnow()
    handle = await env.client.start_workflow(
        DemurrageArc.run,
        DemurrageInput(
            container=CONTAINER,
            terms=demo_terms(),
            discharged_at=discharge,
            auto_approve_limit_usd=Decimal("50"),
            slot_scarcity_prior=0.8,
        ),
        id=f"demurrage::{CONTAINER.container_id}",
        task_queue=TASK_QUEUE,
    )

    # Nothing parked yet - still in the availability-polling phase.
    assert await api_module.pending_approval(CONTAINER.container_id, "demurrage") is None

    # Report availability promptly (no hold, no shift) so phase 1 ends well
    # before the nominal LFD, leaving the pre-LFD checkpoints still ahead of
    # "now" rather than skipped as already-past.
    await env.sleep(timedelta(hours=1))
    await handle.signal(
        DemurrageArc.container_available,
        args=[discharge + timedelta(hours=6), "terminal::avail::MSKU7481920"],
    )

    # Past the first pre-LFD checkpoint (LFD-72h), where risk is assessed with
    # no hold open.
    await env.sleep(timedelta(hours=12))

    pending = await api_module.pending_approval(CONTAINER.container_id, "demurrage")
    assert pending is not None, "expected an option requiring approval by the first checkpoint"
    assert Decimal(pending["cost_usd"]) > Decimal("50")
    assert pending["reason"]

    # Also reachable the way the container detail screen gets it: bundled into
    # /api/container/{cid}/arcs's per-arc entry.
    via_history = await api_module._pending_approval(handle.id)
    assert via_history == pending

    await handle.signal(DemurrageArc.approve, pending["action"])
    await env.sleep(timedelta(seconds=1))

    assert await api_module.pending_approval(CONTAINER.container_id, "demurrage") is None
