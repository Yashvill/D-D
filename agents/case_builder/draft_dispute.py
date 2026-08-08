"""Draft the dispute letter. LLM call #3 of 3.

By the time this runs, every uncited claim has already been removed by
``assemble_evidence``. The model is given only provable material, so the worst it
can do is phrase it badly - it cannot invent a fact, because it was never handed
one.

The amount contested is recomputed in Python from the claims and overwritten on
the result. The LLM is never trusted with arithmetic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from agents.shared import prompts
from agents.shared.llm_client import complete
from agents.shared.models import DisputeLetter, EvidenceChronology

log = logging.getLogger(__name__)

# Below this fraction of the claim, a settlement offer goes to a human.
SETTLEMENT_MANDATE_FRACTION = Decimal("0.70")


def draft(
    chronology: EvidenceChronology,
    *,
    billing_party: str,
    total_billed_usd: Decimal,
    drafted_at: datetime,
    fixture: str | None = "dispute_letter",
) -> DisputeLetter:
    """Draft a dispute letter from an assembled chronology.

    Raises:
        ValueError: If the chronology has no supported claims. There is nothing
            defensible to say, and saying it anyway is how credibility is lost.
    """
    if not chronology.claims:
        raise ValueError(
            f"chronology for invoice {chronology.invoice_id} has no cited claims; "
            f"dropped: {chronology.dropped_claims}"
        )

    claims_block = "\n".join(
        f"{i}. {c.heading}\n"
        f"   amount: ${c.amount_usd}\n"
        f"   argument: {c.argument}\n"
        f"   citations: {', '.join(c.citations)}"
        for i, c in enumerate(chronology.claims, start=1)
    )

    timeline_block = "\n".join(
        f"  {e.occurred_at.isoformat(timespec='minutes')}  {e.kind.value:24}  "
        f"{e.summary}  [{e.source_document_id}]"
        for e in chronology.timeline[:40]
    )

    letter = complete(
        prompts.draft_dispute_prompt(
            container_id=chronology.container_id,
            invoice_id=chronology.invoice_id,
            billing_party=billing_party,
            total_usd=str(total_billed_usd),
            claims_block=claims_block,
            timeline_block=timeline_block,
        ),
        DisputeLetter,
        system=prompts.DRAFT_DISPUTE_SYSTEM,
        fixture=fixture,
        max_tokens=2000,
    )

    # Identity and arithmetic are ours, not the model's.
    letter.invoice_id = chronology.invoice_id
    letter.container_id = chronology.container_id
    letter.amount_contested_usd = chronology.total_claimed_usd
    letter.drafted_at = drafted_at

    # Only citations that actually appear in the assembled claims may survive.
    permitted = {c for claim in chronology.claims for c in claim.citations}
    hallucinated = [c for c in letter.citations if c not in permitted]
    if hallucinated:
        log.warning("stripping %d uncited citation(s) from draft", len(hallucinated))
    letter.citations = sorted(c for c in letter.citations if c in permitted) or sorted(permitted)

    return letter


def evaluate_settlement(
    offer_usd: Decimal,
    claim_usd: Decimal,
    *,
    mandate_fraction: Decimal = SETTLEMENT_MANDATE_FRACTION,
) -> tuple[bool, str]:
    """Decide whether an offer is inside the agent's mandate.

    Bounded settlement authority is a structural guardrail: the agent may accept
    at or above the mandate and must escalate anything below. Enforced in code so
    it can be audited, not in a prompt where it can be talked around.

    Returns:
        ``(may_accept, reason)``
    """
    if claim_usd <= 0:
        return False, "no claim amount to evaluate"

    fraction = offer_usd / claim_usd

    if fraction >= mandate_fraction:
        return True, (
            f"offer ${offer_usd} is {fraction:.0%} of the ${claim_usd} claim, "
            f"at or above the {mandate_fraction:.0%} mandate"
        )

    return False, (
        f"offer ${offer_usd} is {fraction:.0%} of the ${claim_usd} claim, "
        f"below the {mandate_fraction:.0%} mandate; escalating to a human"
    )
