"""Temporal client/worker configuration.

The Pydantic data converter is not optional. Without it the default payload
converter cannot serialise Pydantic models, and - more importantly here -
``date``, ``time`` and ``datetime`` fields do not round-trip at all. This whole
domain is dates: nominal LFD, effective LFD, discharge, gate-out, return slot.

It must be supplied identically to the client, the worker and the time-skipping
test environment, otherwise tests diverge from runtime in ways that are painful
to debug.
"""

from __future__ import annotations

import os

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

TASK_QUEUE_REASONING = "pf-reasoning"
TASK_QUEUE_IO = "pf-io"

# Split queues let LLM activities run at low concurrency with generous timeouts
# while portal scrapes run cheap and aggressive. It also gives fleet-wide rate
# limiting against a carrier API for free, with no coordination between the
# workflows doing the calling.
DEFAULT_TASK_QUEUE = TASK_QUEUE_IO

DATA_CONVERTER = pydantic_data_converter


def target_host() -> str:
    return os.getenv("TEMPORAL_HOST", "localhost:7233")


def namespace() -> str:
    return os.getenv("TEMPORAL_NAMESPACE", "default")


async def connect() -> Client:
    """Connect with the Pydantic converter attached."""
    return await Client.connect(
        target_host(),
        namespace=namespace(),
        data_converter=DATA_CONVERTER,
    )
