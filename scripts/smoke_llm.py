"""Live smoke test for the three real LLM agent tasks.

Runs each of the three genuine LLM calls once against OpenRouter, prints the
parsed, validated result, and reports cache/quota status. Requires
OPENROUTER_API_KEY in .env and LLM_MODE=live.

Usage (from repo root, venv active):
    python scripts/smoke_llm.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.shared.llm_client import LlmError, QuotaExhausted, cache_stats  # noqa: E402
from agents.shared.models import (  # noqa: E402
    Claim,
    Evidence,
    EventKind,
    EvidenceChronology,
)
from agents.watcher.extract_terms import extract_terms  # noqa: E402
from agents.watcher.classify_advisory import classify_advisory  # noqa: E402
from agents.case_builder.draft_dispute import draft  # noqa: E402


SAMPLE_CONTRACT = """\
SERVICE CONTRACT NO. SC-2026-0042  -  PORT OF LOS ANGELES / LONG BEACH

SECTION 7. FREE TIME AND DEMURRAGE
7.1 Merchant shall be allowed four (4) calendar days of free time for import
demurrage, counted from the day after the container is discharged from the
vessel and made available at the marine terminal.
7.2 After expiry of free time, demurrage shall accrue per container per day as
follows:
    Days 1-3 after LFD ..... USD 155 per day
    Days 4-6 after LFD ..... USD 310 per day
    Day 7 and thereafter ... USD 465 per day
Weekends and US federal holidays are counted as chargeable days.

SECTION 8. DETENTION / PER DIEM (EQUIPMENT)
8.1 Five (5) working days of free time are allowed for equipment detention,
counted from gate-out of the loaded container.
8.2 Detention thereafter accrues at USD 120 per container per day for days 1-5
and USD 200 per day thereafter.

SECTION 9. TERMINAL STORAGE
9.1 Terminal storage is billed separately by the marine terminal operator under
its published tariff and is not covered by this contract.
"""

SAMPLE_ADVISORY = """\
CARRIER OPERATIONAL ADVISORY - 12 March 2026
Effective immediately through 18 March 2026, MAERSK will NOT accept empty
returns of 40' high-cube dry containers at the PIER 400 EMPTY DEPOT (Los
Angeles) due to yard congestion. Empties may instead be returned to the ITS
LONG BEACH depot during this window. Reefer and 20' equipment are unaffected.
"""


def banner(text: str) -> None:
    print("\n" + "=" * 70)
    print(text)
    print("=" * 70)


def run_extract_terms() -> None:
    banner("1/3  extract_terms  (real LLM: parse contract prose -> tier tables)")
    terms = extract_terms(
        SAMPLE_CONTRACT,
        port="Los Angeles",
        carrier="MAERSK",
        contract_id="SC-2026-0042",
        source_document_id="contract::SC-2026-0042",
        fixture=None,  # force a real call, no fixture fallback
    )
    print(f"  confidence:      {terms.confidence:.2f}")
    print(f"  clocks found:    {len(terms.clocks)}")
    for clock in terms.clocks.values():
        tiers = ", ".join(
            f"d{t.from_day}-{t.to_day or '∞'}=${t.rate_usd}" for t in clock.tiers
        )
        print(f"    - {clock.charge_type.value:10} free={clock.free_days}d  tiers: {tiers}")
    if terms.extraction_notes:
        print(f"  notes:           {terms.extraction_notes}")


def run_classify_advisory() -> None:
    banner("2/3  classify_advisory  (real LLM: does advisory hit this container?)")
    result = classify_advisory(
        SAMPLE_ADVISORY,
        depot="PIER 400 EMPTY DEPOT",
        carrier="MAERSK",
        container_type="40HC",
        fixture=None,
    )
    print(f"  affects_this_container: {result.affects_this_container}")
    print(f"  confidence:             {result.confidence:.2f}")
    print(f"  reasoning:              {result.reasoning}")


def run_draft_dispute() -> None:
    banner("3/3  draft_dispute  (real LLM: write letter from cited claims only)")
    chronology = EvidenceChronology(
        container_id="MSKU7481920",
        invoice_id="INV-DEM-88431",
        assembled_at=datetime(2026, 4, 5, 9, 0),
        billed_total_usd=Decimal("3150"),
        claims=[
            Claim(
                heading="Effective last free day understated",
                argument=(
                    "Container discharged 3 Mar but not grounded/appointable until "
                    "5 Mar; two free days consumed by terminal handling."
                ),
                amount_usd=Decimal("620"),
                citations=["terminal::availability::MSKU7481920"],
            ),
            Claim(
                heading="Charges billed during no-appointment window",
                argument=(
                    "On 13-15 Mar the appointment system offered no slot despite "
                    "logged attempts; these top-tier days are a carrier capacity failure."
                ),
                amount_usd=Decimal("1395"),
                citations=["terminal::appointments::MSKU7481920"],
            ),
        ],
        timeline=[
            Evidence(
                kind=EventKind.DISCHARGED,
                occurred_at=datetime(2026, 3, 3, 8, 0),
                source_system="terminal",
                source_document_id="terminal::availability::MSKU7481920",
                summary="Container discharged from vessel",
            ),
            Evidence(
                kind=EventKind.CONTAINER_AVAILABLE,
                occurred_at=datetime(2026, 3, 5, 14, 0),
                source_system="terminal",
                source_document_id="terminal::availability::MSKU7481920",
                summary="Container grounded and appointable",
            ),
        ],
    )
    letter = draft(
        chronology,
        billing_party="MAERSK",
        total_billed_usd=Decimal("3150"),
        drafted_at=datetime(2026, 4, 5, 9, 15),
        fixture=None,
    )
    print(f"  subject:            {letter.subject}")
    print(f"  amount_contested:   ${letter.amount_contested_usd}")
    print(f"  citations:          {letter.citations}")
    print(f"  body (first 400ch):\n{letter.body[:400]}")


def main() -> int:
    print("LLM smoke test  |  mode =", os.getenv("LLM_MODE", "mock"))
    if os.getenv("LLM_MODE", "mock").lower() != "live":
        print("\nWARNING: LLM_MODE is not 'live'. Set LLM_MODE=live to hit OpenRouter.")

    failures: list[str] = []
    for name, fn in (
        ("extract_terms", run_extract_terms),
        ("classify_advisory", run_classify_advisory),
        ("draft_dispute", run_draft_dispute),
    ):
        try:
            fn()
        except QuotaExhausted as exc:
            print(f"\n  QUOTA EXHAUSTED on {name}: {exc}")
            failures.append(name)
            break
        except (LlmError, Exception) as exc:  # noqa: BLE001
            print(f"\n  FAILED {name}: {type(exc).__name__}: {exc}")
            failures.append(name)

    banner("cache / quota status")
    for k, v in cache_stats().items():
        print(f"  {k}: {v}")

    if failures:
        print(f"\nRESULT: {len(failures)} task(s) failed: {failures}")
        return 1
    print("\nRESULT: all 3 live LLM tasks passed and validated against Pydantic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
