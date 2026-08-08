"""Worker entrypoint.

Run with:
    python -m temporal_backend.main

Requires ``temporal server start-dev`` in another terminal.

Split task queues are the intent here: LLM activities want low concurrency and
generous timeouts, portal scrapes want cheap and aggressive retries. Running one
queue keeps the demo simple; the ``--queue`` flag exists so a second worker can
take the reasoning queue when you want to show the split.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from temporalio.worker import Worker

from agents.shared.llm_client import cache_stats
from temporal_backend.activities.registry import ALL_ACTIVITIES
from temporal_backend.shared.converter import (
    TASK_QUEUE_IO,
    TASK_QUEUE_REASONING,
    connect,
)
from temporal_backend.workflows.container import ContainerWorkflow
from temporal_backend.workflows.demurrage import DemurrageArc
from temporal_backend.workflows.detention import DetentionArc
from temporal_backend.workflows.dispute import DisputeArc

ALL_WORKFLOWS = [ContainerWorkflow, DemurrageArc, DetentionArc, DisputeArc]

log = logging.getLogger("pf.worker")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent Fleet worker")
    parser.add_argument(
        "--queue",
        default=TASK_QUEUE_IO,
        choices=[TASK_QUEUE_IO, TASK_QUEUE_REASONING],
        help="task queue to serve",
    )
    parser.add_argument(
        "--max-activities",
        type=int,
        default=None,
        help="concurrent activity cap; use a low value on the reasoning queue",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    stats = cache_stats()
    log.info(
        "LLM mode=%s model=%s cached=%s fixtures=%s",
        stats["mode"],
        stats["model"],
        stats["cached_responses"],
        stats["fixtures"],
    )
    if stats["mode"] == "live":
        log.warning(
            "LLM_MODE=live - OpenRouter free tier allows 50 requests/day and "
            "failed requests still count. Warm the cache, then switch to cache."
        )

    client = await connect()

    cap = args.max_activities
    if cap is None:
        cap = 4 if args.queue == TASK_QUEUE_REASONING else 50

    log.info("worker starting on %s (max %d concurrent activities)", args.queue, cap)

    async with Worker(
        client,
        task_queue=args.queue,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
        max_concurrent_activities=cap,
    ):
        log.info("worker ready. ctrl-c to stop.")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker stopped")
