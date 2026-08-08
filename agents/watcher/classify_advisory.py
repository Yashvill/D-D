"""Classify a carrier advisory against one container. LLM call #2 of 3.

The difference between leak 04 happening and not. An advisory lands in a shared
inbox announcing that empties will not be accepted at some depot; deciding
whether it applies to *this* container at *this* depot is genuine judgement over
prose, which is why this one earns an LLM.

The asymmetry matters and is encoded in the prompt: a false positive costs one
wasted check, a false negative costs the entire detention charge. When the depot
is ambiguous, say yes with low confidence.
"""

from __future__ import annotations

import logging

from agents.shared import prompts
from agents.shared.llm_client import complete
from agents.shared.models import AdvisoryClassification

log = logging.getLogger(__name__)

LOW_CONFIDENCE = 0.60


def classify_advisory(
    advisory_text: str,
    *,
    depot: str,
    carrier: str,
    container_type: str = "40HC",
    fixture: str | None = "advisory_classification",
) -> AdvisoryClassification:
    """Decide whether an advisory restricts empty returns for this container."""
    result = complete(
        prompts.classify_advisory_prompt(advisory_text, depot, carrier, container_type),
        AdvisoryClassification,
        system=prompts.CLASSIFY_ADVISORY_SYSTEM,
        fixture=fixture,
    )

    if result.affects_this_container and result.confidence < LOW_CONFIDENCE:
        log.warning(
            "advisory matched %s at low confidence %.2f; escalating rather than acting",
            depot,
            result.confidence,
        )

    return result


def requires_escalation(result: AdvisoryClassification) -> bool:
    """Ambiguous matches go to a human. Mandatory escalation on anomaly."""
    return result.affects_this_container and result.confidence < LOW_CONFIDENCE
