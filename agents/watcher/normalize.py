"""Turn a mess of formats into one clean event model.

Rule-based. The genuinely hard part is identity resolution: the same container
appears as ``MSKU7481920``, ``MSKU 748192-0`` and "the Ningbo box" across four
systems. That is a normalisation problem with a check digit, not a reasoning
problem, so it does not need an LLM.

Three fields make an event usable: ``occurred_at`` (when it happened in the
world, not when we learned of it), ``source_system``, and
``source_document_id``. An event without the third is a rumour and cannot appear
in a dispute.
"""

from __future__ import annotations

import re
from datetime import datetime

from agents.shared.models import ContainerEvent, EventKind

# ISO 6346: four letters (owner code + category) then seven digits.
_CONTAINER_RE = re.compile(r"([A-Z]{4})[\s\-]?(\d{6})[\s\-]?(\d)", re.IGNORECASE)

# EDI 315 status codes to our event vocabulary.
EDI_315_STATUS: dict[str, EventKind] = {
    "VA": EventKind.DISCHARGED,
    "UV": EventKind.DISCHARGED,
    "OA": EventKind.CONTAINER_AVAILABLE,
    "AV": EventKind.CONTAINER_AVAILABLE,
    "OC": EventKind.GATE_OUT,
    "AE": EventKind.GATE_OUT,
    "RD": EventKind.EMPTY_RETURNED,
    "AY": EventKind.EMPTY_RETURNED,
    "I": EventKind.HOLD_PLACED,
    "X": EventKind.HOLD_RELEASED,
}


class IdentityError(ValueError):
    pass


def normalise_container_id(raw: str) -> str:
    """Canonicalise any rendering of a container number to ``MSKU7481920``.

    Raises:
        IdentityError: If no ISO 6346 identifier can be found.
    """
    if not raw:
        raise IdentityError("empty container reference")

    match = _CONTAINER_RE.search(raw.upper().strip())
    if not match:
        raise IdentityError(f"no ISO 6346 container number in {raw!r}")

    owner, serial, check = match.groups()
    return f"{owner}{serial}{check}"


def iso6346_check_digit(container_id: str) -> int:
    """Compute the ISO 6346 check digit, for catching transcription errors."""
    alphabet = "0123456789A?BCDEFGHIJK?LMNOPQRSTU?VWXYZ"
    total = 0
    for position, char in enumerate(container_id[:10].upper()):
        value = alphabet.index(char) if char.isalpha() else int(char)
        total += value * (2**position)
    return total % 11 % 10


def is_valid_container_id(container_id: str) -> bool:
    if len(container_id) != 11:
        return False
    try:
        return iso6346_check_digit(container_id) == int(container_id[10])
    except (ValueError, IndexError):
        return False


def normalise_edi_315(
    raw: dict[str, str],
    *,
    source_document_id: str | None = None,
) -> ContainerEvent:
    """Map an EDI 315 container status message onto a ``ContainerEvent``.

    Args:
        raw: Must carry ``status_code``, ``container``, ``event_time``. May carry
            ``location`` and ``document_id``.
    """
    status = raw.get("status_code", "").strip().upper()
    kind = EDI_315_STATUS.get(status)
    if kind is None:
        raise ValueError(f"unmapped EDI 315 status code {status!r}")

    container_id = normalise_container_id(raw["container"])
    occurred_at = _parse_time(raw["event_time"])
    doc_id = source_document_id or raw.get("document_id") or f"edi315::{status}::{container_id}"

    payload = {
        "status_code": status,
        "container_id": container_id,
    }
    if "location" in raw:
        payload["location"] = raw["location"]

    return ContainerEvent(
        kind=kind,
        occurred_at=occurred_at,
        source_system="EDI_315",
        source_document_id=doc_id,
        payload=payload,
    )


def _parse_time(value: str) -> datetime:
    """Accept the handful of timestamp shapes these feeds actually emit."""
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in ("%Y%m%d%H%M", "%Y%m%d %H%M", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    raise ValueError(f"unparseable timestamp {value!r}")


def detect_anomalies(events: list[ContainerEvent]) -> list[str]:
    """Flag contradictions and missing expected events.

    Mandatory escalation on anomaly is a guardrail, so this must be mechanical
    rather than a judgement call.
    """
    problems: list[str] = []
    by_kind = {e.kind: e for e in events}

    ordering = [
        (EventKind.DISCHARGED, EventKind.CONTAINER_AVAILABLE),
        (EventKind.CONTAINER_AVAILABLE, EventKind.GATE_OUT),
        (EventKind.GATE_OUT, EventKind.EMPTY_RETURNED),
    ]
    for earlier, later in ordering:
        a, b = by_kind.get(earlier), by_kind.get(later)
        if a and b and b.occurred_at < a.occurred_at:
            problems.append(
                f"{later.value} at {b.occurred_at.isoformat()} precedes "
                f"{earlier.value} at {a.occurred_at.isoformat()}"
            )

    if EventKind.GATE_OUT in by_kind and EventKind.DISCHARGED not in by_kind:
        problems.append("gate_out recorded with no preceding discharge")

    uncitable = [e.kind.value for e in events if not e.is_citable]
    if uncitable:
        problems.append(f"events without a source_document_id: {', '.join(sorted(set(uncitable)))}")

    return problems
