"""DBOS workflows.

Importing this package registers workflow decorators with DBOS. The
entrypoint separately calls `register_queues()` (needs settings loaded
first) and `seed_cursor()` (needs DriveClient) before `DBOS.launch()`.
"""

from __future__ import annotations

from dbos import DBOS

from sn2md_worker.config import get_settings
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.workflows.convert_note import convert_note, convert_note_impl
from sn2md_worker.workflows.poll_changes import (
    CONVERT_QUEUE_NAME,
    poll_changes,
    poll_changes_impl,
    seed_cursor,
)

POLL_QUEUE_NAME = "poll_queue"

__all__ = [
    "CONVERT_QUEUE_NAME",
    "POLL_QUEUE_NAME",
    "convert_note",
    "convert_note_impl",
    "poll_changes",
    "poll_changes_impl",
    "register_queues",
    "seed_cursor",
    "seed_cursor_if_ready",
]


def register_queues() -> None:
    """Register DBOS queues. Call after set_settings(), before DBOS.launch()."""
    settings = get_settings()
    DBOS.register_queue(
        CONVERT_QUEUE_NAME,
        worker_concurrency=settings.queue.convert_concurrency,
    )
    DBOS.register_queue(POLL_QUEUE_NAME, worker_concurrency=1)


def seed_cursor_if_ready(drive: DriveClient | None) -> None:
    """Seed drive_change_cursor if the DriveClient is available."""
    if drive is None:
        return
    seed_cursor(drive)
