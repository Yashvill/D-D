"""HTTP surface over the running workflows.

There is deliberately no database here. Every read is a Temporal query against a
live (or closed) execution, so the workflow state *is* the read model. That is
the claim the control tower makes, and this module is what makes it true rather
than illustrative.

Run with:
    uvicorn temporal_backend.api:app --reload --port 8000

Requires ``temporal server start-dev`` and ``python -m temporal_backend.main``.

Two things matter for correctness:

*   The client must be built by ``shared.converter.connect()``. A bare
    ``Client.connect`` omits the Pydantic data converter, and every ``date`` and
    ``datetime`` in these payloads silently fails to round-trip. That is the
    failure ``tests/test_converter_gate.py`` exists to catch.
*   Signals are dispatched through an explicit whitelist. A client string is
    never passed to ``handle.signal``, and every payload is validated by the
    same domain models the workflows themselves use - all of which set
    ``extra="forbid"``, so a malformed body is rejected rather than half-applied.

Set ``PF_API_READONLY=1`` to disable every write route.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from temporalio.api.enums.v1 import EventType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from agents.shared.models import ContainerInput, Hold, HoldType, Invoice
from temporal_backend.activities.registry import NotifyHumanInput
from temporal_backend.shared.converter import TASK_QUEUE_IO, connect, namespace
from temporal_backend.workflows.container import ContainerWorkflow, ContainerWorkflowInput

READONLY = os.getenv("PF_API_READONLY", "") == "1"

_client: Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One client for the process. Connecting per request would be wasteful and
    would also lose the converter guarantee in a less obvious place."""
    global _client
    _client = await connect()
    yield
    _client = None


app = FastAPI(title="Persistent Fleet control tower", lifespan=lifespan)


def client() -> Client:
    if _client is None:  # pragma: no cover - lifespan guarantees this
        raise HTTPException(503, "temporal client not initialised")
    return _client


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------

_CLOSED = ("already completed", "workflow execution already completed")
_MISSING = ("not found", "no such workflow")
_UNREACHABLE = ("deadline exceeded", "unavailable", "connection refused", "no poller")


def _rpc_error(exc: RPCError, wf_id: str) -> HTTPException:
    """Map Temporal RPC failures onto statuses the UI can act on.

    The distinction that matters to a viewer is "this container does not exist"
    (404) versus "the system that answers for it is down" (503). Collapsing both
    into one error is how dashboards end up showing zeros as though they were
    real values.
    """
    msg = str(exc).lower()
    if any(m in msg for m in _MISSING):
        return HTTPException(404, f"{wf_id} not found")
    if any(m in msg for m in _UNREACHABLE):
        return HTTPException(503, "temporal unreachable, or no worker is polling")
    if any(m in msg for m in _CLOSED):
        return HTTPException(409, f"{wf_id} has already closed")
    return HTTPException(502, f"{wf_id}: {exc}")


async def _query(wf_id: str, name: str) -> Any:
    try:
        return await client().get_workflow_handle(wf_id).query(name)
    except RPCError as exc:
        raise _rpc_error(exc, wf_id) from exc


async def _query_optional(wf_id: str, name: str, *, run_id: str | None = None) -> Any | None:
    """For arcs that may not have spawned yet - absence is normal, not an error.

    ``run_id`` pins the query to one specific execution. Without it,
    ``get_workflow_handle`` resolves to whichever run is *currently* latest for
    that workflow id - harmless for a single-container lookup (that is exactly
    the "current state" a detail screen wants), but wrong when iterating
    historical rows from ``list_workflows``: every closed row would silently
    echo the newest run's live state instead of its own.
    """
    try:
        return await client().get_workflow_handle(wf_id, run_id=run_id).query(name)
    except RPCError:
        return None


async def _status(wf_id: str) -> str | None:
    try:
        desc = await client().get_workflow_handle(wf_id).describe()
        return desc.status.name if desc.status else None
    except RPCError:
        return None


async def _pending_approval(wf_id: str, *, signal: str = "approve") -> dict[str, Any] | None:
    """The most recent notify_human call not yet answered by its approve signal.

    There is no query for this - ``notify_human`` only logs, and ``state()``
    was deliberately left untouched beyond the one accepted ``letter_draft``
    addition. But the workflow's own gate (``choice.action in self.approvals``)
    is exactly "does an approve signal for this action exist", and Temporal's
    history already records both the activity input and every signal. Reading
    history instead of adding a query gets the same answer for free.

    ``signal`` differs by arc: the demurrage and detention arcs gate on
    ``approve``, while ``DisputeArc`` gates on ``approve_settlement``. Matching
    the wrong name would leave a genuinely parked settlement invisible.
    """
    try:
        history = await client().get_workflow_handle(wf_id).fetch_history()
    except RPCError:
        return None

    pending: dict[str, Any] | None = None
    approved: set[str] = set()
    conv = client().data_converter.payload_converter
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            if attrs.activity_type.name != "notify_human":
                continue
            (inp,) = conv.from_payloads(
                list(attrs.input.payloads), type_hints=[NotifyHumanInput]
            )
            pending = {
                "action": inp.action,
                "cost_usd": str(inp.cost_usd),
                "reason": inp.reason,
                "detail": inp.detail,
                "requested_at": (
                    event.event_time.ToDatetime().isoformat() if event.event_time else None
                ),
            }
        elif event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            attrs = event.workflow_execution_signaled_event_attributes
            if attrs.signal_name != signal:
                continue
            (action,) = conv.from_payloads(list(attrs.input.payloads), type_hints=[str])
            approved.add(action)

    if pending and pending["action"] in approved:
        return None
    return pending


# --------------------------------------------------------------------------
# ids
# --------------------------------------------------------------------------


def _container_id(cid: str) -> str:
    return f"container::{cid}"


def _arc_id(arc: str, cid: str) -> str:
    if arc not in ("demurrage", "detention"):
        raise HTTPException(400, "arc must be 'demurrage' or 'detention'")
    return f"{arc}::{cid}"


def _dispute_id(invoice_id: str) -> str:
    return f"dispute::{invoice_id}"


# --------------------------------------------------------------------------
# read routes
# --------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    """Server reachability plus whether a worker is actually polling.

    A reachable server with no worker is the most common broken state in a demo,
    and it looks identical to a healthy one from the client's side until a query
    hangs. Reporting poller count makes it obvious.
    """
    out: dict[str, Any] = {"server": False, "workers": 0, "task_queue": TASK_QUEUE_IO}
    try:
        await client().service_client.check_health()
        out["server"] = True
    except Exception as exc:  # noqa: BLE001 - health must never raise
        out["error"] = str(exc)
        return out

    try:
        resp = await client().service_client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=namespace(), task_queue=TaskQueue(name=TASK_QUEUE_IO)
            )
        )
        out["workers"] = len(resp.pollers)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    out["readonly"] = READONLY
    return out


@app.get("/api/fleet")
async def fleet() -> list[dict]:
    """One row per container: its most recent run, with live state attached.

    ``list_workflows`` returns every run that has ever existed for a workflow
    id, not just the current one - a container restarted for a demo repeats
    under the same id, so without deduplication the fleet view fills up with
    that container's own history rendered as though each run were a distinct
    box.
    """
    latest: dict[str, Any] = {}
    try:
        async for wf in client().list_workflows("WorkflowType = 'ContainerWorkflow'"):
            existing = latest.get(wf.id)
            if existing is None or (wf.start_time and (existing.start_time is None or wf.start_time > existing.start_time)):
                latest[wf.id] = wf
    except RPCError as exc:
        raise _rpc_error(exc, "fleet") from exc

    items: list[dict] = []
    for wf in latest.values():
        entry: dict[str, Any] = {
            "workflow_id": wf.id,
            "container_id": wf.id.removeprefix("container::"),
            "status": wf.status.name if wf.status else None,
            "started_at": wf.start_time.isoformat() if wf.start_time else None,
        }
        entry["state"] = await _query_optional(wf.id, "state", run_id=wf.run_id)
        items.append(entry)
    items.sort(key=lambda e: e["started_at"] or "", reverse=True)
    return items


@app.get("/api/container/{cid}")
async def container(cid: str) -> dict:
    wf_id = _container_id(cid)
    return {
        "container_id": cid,
        "workflow_id": wf_id,
        "status": await _status(wf_id),
        "state": await _query(wf_id, "state"),
    }


@app.get("/api/container/{cid}/evidence")
async def evidence(cid: str) -> list[dict]:
    """The cited event log. Every row carries a source_document_id by construction."""
    return await _query(_container_id(cid), "evidence_log")


@app.get("/api/container/{cid}/arcs")
async def arcs(cid: str) -> dict:
    """Parent plus children in one call, so the detail screen polls once."""
    parent_id = _container_id(cid)
    parent_state = await _query(parent_id, "state")

    out: dict[str, Any] = {
        "container": {
            "workflow_id": parent_id,
            "status": await _status(parent_id),
            "state": parent_state,
        },
        "demurrage": None,
        "detention": None,
        "disputes": [],
    }

    for arc in ("demurrage", "detention"):
        wf_id = f"{arc}::{cid}"
        state = await _query_optional(wf_id, "state")
        if state is not None:
            out[arc] = {
                "workflow_id": wf_id,
                "status": await _status(wf_id),
                "state": state,
                "pending_approval": await _pending_approval(wf_id),
            }

    for invoice_id in parent_state.get("disputes", []):
        wf_id = invoice_id if invoice_id.startswith("dispute::") else _dispute_id(invoice_id)
        state = await _query_optional(wf_id, "state")
        if state is None:
            continue
        out["disputes"].append(
            {
                "workflow_id": wf_id,
                "invoice_id": wf_id.removeprefix("dispute::"),
                "status": await _status(wf_id),
                "state": state,
                "letter": await _query_optional(wf_id, "letter_draft"),
                "pending_approval": await _pending_approval(wf_id, signal="approve_settlement"),
            }
        )

    return out


@app.get("/api/dispute/{invoice_id}")
async def dispute(invoice_id: str) -> dict:
    wf_id = _dispute_id(invoice_id)
    return {
        "invoice_id": invoice_id,
        "workflow_id": wf_id,
        "status": await _status(wf_id),
        "state": await _query(wf_id, "state"),
        "findings": await _query_optional(wf_id, "findings") or [],
        "letter": await _query_optional(wf_id, "letter_draft"),
    }


@app.get("/api/container/{cid}/arc/{arc}/pending_approval")
async def pending_approval(cid: str, arc: str) -> dict | None:
    return await _pending_approval(_arc_id(arc, cid))


@app.get("/api/dispute/{invoice_id}/pending_approval")
async def dispute_pending_approval(invoice_id: str) -> dict | None:
    return await _pending_approval(_dispute_id(invoice_id), signal="approve_settlement")


@app.get("/api/approvals")
async def approvals() -> list[dict]:
    """Every arc, fleet-wide, genuinely parked on a human decision right now.

    Two plain ``list_workflows`` calls rather than one compound query - the dev
    server's SQLite visibility store does not reliably support OR across
    ``WorkflowType``.
    """
    out: list[dict] = []
    targets = (
        ("DemurrageArc", "demurrage", "approve"),
        ("DetentionArc", "detention", "approve"),
        # The settlement gate. Its signal name differs, and its container has to
        # come from parent_id because a dispute is keyed by invoice, not box.
        ("DisputeArc", "dispute", "approve_settlement"),
    )
    for wf_type, arc, signal in targets:
        try:
            async for wf in client().list_workflows(f"WorkflowType = '{wf_type}'"):
                if not wf.status or wf.status.name != "RUNNING":
                    continue
                pending = await _pending_approval(wf.id, signal=signal)
                if pending is None:
                    continue
                entry = {
                    "workflow_id": wf.id,
                    "container_id": (wf.parent_id or "").removeprefix("container::")
                    if arc == "dispute"
                    else wf.id.removeprefix(f"{arc}::"),
                    "arc": arc,
                    **pending,
                }
                if arc == "dispute":
                    entry["invoice_id"] = wf.id.removeprefix("dispute::")
                out.append(entry)
        except RPCError as exc:
            raise _rpc_error(exc, "approvals") from exc
    return out


# --------------------------------------------------------------------------
# signal whitelists
# --------------------------------------------------------------------------

Builder = Callable[[dict[str, Any]], list[Any]]


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(422, f"not an ISO-8601 datetime: {value!r}") from exc


def _req(body: dict[str, Any], key: str) -> Any:
    if key not in body:
        raise HTTPException(422, f"missing field: {key}")
    return body[key]


def _model(body: dict[str, Any], key: str, model: type[BaseModel]) -> Any:
    """Validate with the workflow's own model.

    These models set ``extra='forbid'``, so a typo in a field name is a 422 here
    rather than a confusing rejection inside the workflow.
    """
    try:
        return model.model_validate(_req(body, key))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - pydantic error detail is the message
        raise HTTPException(422, f"invalid {key}: {exc}") from exc


CONTAINER_SIGNALS: dict[str, Builder] = {
    "customs_entry_filed": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "discharged": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "invoice_received": lambda b: [_model(b, "invoice", Invoice)],
    "paid_under_protest": lambda b: [_req(b, "invoice_id"), _dt(_req(b, "at"))],
    "close": lambda b: [],
}

DEMURRAGE_SIGNALS: dict[str, Builder] = {
    "container_available": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "hold_placed": lambda b: [_model(b, "hold", Hold)],
    "hold_released": lambda b: [
        HoldType(_req(b, "hold_type")),
        _dt(_req(b, "at")),
        _req(b, "source_document_id"),
    ],
    "gate_out": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "approve": lambda b: [_req(b, "action")],
}

DETENTION_SIGNALS: dict[str, Builder] = {
    "cargo_stripped": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "carrier_advisory": lambda b: [
        _req(b, "advisory_text"),
        _req(b, "source_document_id"),
        _dt(_req(b, "at")),
    ],
    "empty_returned": lambda b: [_dt(_req(b, "at")), _req(b, "source_document_id")],
    "approve": lambda b: [_req(b, "action")],
}

DISPUTE_SIGNALS: dict[str, Builder] = {
    "carrier_replied": lambda b: [
        _req(b, "message"),
        Decimal(str(b["offer_usd"])) if b.get("offer_usd") is not None else None,
    ],
    "approve_settlement": lambda b: [_req(b, "action")],
    "settled": lambda b: [Decimal(str(_req(b, "amount_usd")))],
}


def _guard_writes() -> None:
    if READONLY:
        raise HTTPException(403, "PF_API_READONLY=1; write routes are disabled")


async def _signal(wf_id: str, name: str, table: dict[str, Builder], body: dict) -> dict:
    _guard_writes()
    if name not in table:
        raise HTTPException(
            404, f"unknown signal {name!r}; allowed: {sorted(table)}"
        )
    args = table[name](body or {})
    try:
        await client().get_workflow_handle(wf_id).signal(name, args=args)
    except RPCError as exc:
        raise _rpc_error(exc, wf_id) from exc
    return {"ok": True, "workflow_id": wf_id, "signal": name}


@app.post("/api/container/{cid}/signal/{name}")
async def signal_container(cid: str, name: str, body: dict | None = None) -> dict:
    return await _signal(_container_id(cid), name, CONTAINER_SIGNALS, body or {})


@app.post("/api/container/{cid}/arc/{arc}/signal/{name}")
async def signal_arc(cid: str, arc: str, name: str, body: dict | None = None) -> dict:
    wf_id = _arc_id(arc, cid)
    table = DEMURRAGE_SIGNALS if arc == "demurrage" else DETENTION_SIGNALS
    return await _signal(wf_id, name, table, body or {})


@app.post("/api/dispute/{invoice_id}/signal/{name}")
async def signal_dispute(invoice_id: str, name: str, body: dict | None = None) -> dict:
    return await _signal(_dispute_id(invoice_id), name, DISPUTE_SIGNALS, body or {})


# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------


class StartRequest(BaseModel):
    container: ContainerInput
    contract_text: str = ""
    auto_approve_limit_usd: Decimal = Decimal("250")
    slot_scarcity_prior: float = Field(default=0.8, ge=0.0, le=1.0)
    depot_restriction_prior: float = Field(default=0.6, ge=0.0, le=1.0)
    fresh: bool = False


@app.post("/api/container/start")
async def start(req: StartRequest) -> dict:
    """Start a ContainerWorkflow, optionally clearing a previous run first.

    ``fresh`` terminates the parent and its arcs, which is what makes the console
    re-runnable without dropping to a shell.
    """
    _guard_writes()
    cid = req.container.container_id
    wf_id = req.container.workflow_id

    if req.fresh:
        await _clear_container(cid, reason="console restart")

    try:
        await client().start_workflow(
            ContainerWorkflow.run,
            ContainerWorkflowInput(
                container=req.container,
                contract_text=req.contract_text,
                auto_approve_limit_usd=req.auto_approve_limit_usd,
                slot_scarcity_prior=req.slot_scarcity_prior,
                depot_restriction_prior=req.depot_restriction_prior,
            ),
            id=wf_id,
            task_queue=TASK_QUEUE_IO,
        )
    except WorkflowAlreadyStartedError:
        return {"ok": True, "workflow_id": wf_id, "already_running": True}
    except RPCError as exc:
        raise _rpc_error(exc, wf_id) from exc

    return {"ok": True, "workflow_id": wf_id, "already_running": False}


async def _clear_container(cid: str, *, reason: str) -> list[str]:
    """Terminate a container, its arcs, and any dispute that outlived them.

    Disputes are the subtle part. ``DisputeArc`` is started with
    ``ParentClosePolicy.ABANDON`` precisely so it survives its parent - that is
    the point of the design - which also means terminating the container does
    not touch it. A restarted container then receives the same invoice, tries
    to start ``dispute::<invoice_id>`` again, and the *parent* dies with
    "Workflow execution already started".

    Orphans are found by ``parent_id`` rather than by guessing invoice ids:
    Temporal records the parent on the child's visibility entry even when the
    close policy is ABANDON, so this stays correct for invoices this endpoint
    has never seen.
    """
    killed: list[str] = []

    victims = [_container_id(cid), f"demurrage::{cid}", f"detention::{cid}"]
    try:
        async for wf in client().list_workflows("WorkflowType = 'DisputeArc'"):
            if wf.parent_id == _container_id(cid) and wf.status and wf.status.name == "RUNNING":
                victims.append(wf.id)
    except RPCError:
        pass  # best-effort: a visibility hiccup must not block the restart

    for victim in victims:
        try:
            await client().get_workflow_handle(victim).terminate(reason=reason)
            killed.append(victim)
        except RPCError:
            pass
    return killed


@app.post("/api/container/{cid}/terminate")
async def terminate(cid: str) -> dict:
    """Clear a run, its arcs, and any abandoned dispute still holding its id."""
    _guard_writes()
    return {"ok": True, "terminated": await _clear_container(cid, reason="console terminate")}


@app.get("/api/meta/signals")
async def meta_signals() -> dict:
    """What the journey driver renders its buttons from."""
    return {
        "container": sorted(CONTAINER_SIGNALS),
        "demurrage": sorted(DEMURRAGE_SIGNALS),
        "detention": sorted(DETENTION_SIGNALS),
        "dispute": sorted(DISPUTE_SIGNALS),
        "readonly": READONLY,
    }


@app.exception_handler(RPCError)
async def rpc_handler(_request, exc: RPCError) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=502, content={"detail": str(exc)})
