"""Extract contract terms from the service contract PDF. LLM call #1 of 3.

The single highest-leverage activity in the entire 45 days: it produces the fact
nobody currently has, and every later calculation depends on it. It runs on day
one, when there is no time pressure at all, which is exactly why a low-confidence
extraction can be routed to a human for free.

The free-tier context window is smaller than the paid variant, so long contracts
are chunked to the sections that actually carry the terms rather than sent whole.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from agents.shared import prompts
from agents.shared.llm_client import complete
from agents.shared.models import ContractTerms

log = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.70

# Sections worth sending. Free endpoints truncate, so relevance beats volume.
_RELEVANT = re.compile(
    r"(free\s+time|demurrage|detention|per\s+diem|storage|chassis|tariff|"
    r"last\s+free\s+day|tier|weekend|holiday|allowance)",
    re.IGNORECASE,
)


def read_contract_pdf(path: str | Path) -> str:
    """Extract text from a contract PDF."""
    import fitz  # PyMuPDF, imported lazily so tests need not install it

    doc = fitz.open(str(path))
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def select_relevant_text(contract_text: str, *, max_chars: int = 12_000) -> str:
    """Keep only paragraphs that mention terms, to fit a free-tier context window.

    Returns the whole text when it already fits.
    """
    if len(contract_text) <= max_chars:
        return contract_text

    paragraphs = re.split(r"\n\s*\n", contract_text)
    kept: list[str] = []
    size = 0

    for para in paragraphs:
        if not _RELEVANT.search(para):
            continue
        if size + len(para) > max_chars:
            break
        kept.append(para.strip())
        size += len(para)

    if not kept:
        log.warning("no term-bearing paragraphs matched; falling back to a head slice")
        return contract_text[:max_chars]

    return "\n\n".join(kept)


def extract_terms(
    contract_text: str,
    *,
    port: str,
    carrier: str,
    contract_id: str,
    source_document_id: str,
    fixture: str | None = "contract_terms",
) -> ContractTerms:
    """Parse free time, tier tables and counting rules into structured state.

    Term extraction will sometimes be wrong, so the model is asked for a
    confidence score and anything below threshold is annotated for human review
    on day 1 rather than discovered on day 40.
    """
    relevant = select_relevant_text(contract_text)

    terms = complete(
        prompts.extract_terms_prompt(relevant, port, carrier),
        ContractTerms,
        system=prompts.EXTRACT_TERMS_SYSTEM,
        fixture=fixture,
    )

    # The model does not get to decide its own identity fields.
    terms.contract_id = contract_id
    terms.port = port
    terms.carrier = carrier
    terms.source_document_id = source_document_id

    if terms.confidence < CONFIDENCE_THRESHOLD:
        note = (
            f"confidence {terms.confidence:.2f} below {CONFIDENCE_THRESHOLD:.2f}; "
            f"route to human review while there is no time pressure"
        )
        terms.extraction_notes = [*terms.extraction_notes, note]
        log.warning("contract %s: %s", contract_id, note)

    return terms


def needs_human_review(terms: ContractTerms) -> bool:
    return terms.confidence < CONFIDENCE_THRESHOLD
