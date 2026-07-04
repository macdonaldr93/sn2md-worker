"""DBOS workflows.

Importing this package registers workflow decorators with DBOS. The
entrypoint separately calls the register_/ensure_ helpers below in the
correct order after `DBOS.launch()`.
"""

from __future__ import annotations

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.workflows.backfill import backfill, backfill_impl
from sn2md_worker.workflows.convert_note import convert_note, convert_note_impl
from sn2md_worker.workflows.delete_output import delete_output, delete_output_impl
from sn2md_worker.workflows.poll_changes import (
    CONVERT_QUEUE_NAME,
    poll_changes,
    poll_changes_impl,
    seed_cursor,
)
from sn2md_worker.workflows.renew_watch import (
    ensure_active_channel,
    renew_watch_channel,
    renew_watch_channel_impl,
)

POLL_QUEUE_NAME = "poll_queue"
RENEW_SCHEDULE_NAME = "renew-watch-channel"
RENEW_SCHEDULE_CRON = "0 6 * * *"  # daily at 06:00 UTC

__all__ = [
    "CONVERT_QUEUE_NAME",
    "POLL_QUEUE_NAME",
    "RENEW_SCHEDULE_CRON",
    "RENEW_SCHEDULE_NAME",
    "backfill",
    "backfill_impl",
    "convert_note",
    "convert_note_impl",
    "delete_output",
    "delete_output_impl",
    "enqueue_startup_backfill",
    "ensure_active_channel",
    "poll_changes",
    "poll_changes_impl",
    "register_queues",
    "register_schedules",
    "renew_watch_channel",
    "renew_watch_channel_impl",
    "seed_cursor",
    "seed_cursor_if_ready",
]


def register_queues() -> None:
    """Register DBOS queues. Call after DBOS.launch()."""
    settings = get_settings()
    DBOS.register_queue(
        CONVERT_QUEUE_NAME,
        worker_concurrency=settings.queue.convert_concurrency,
    )
    DBOS.register_queue(POLL_QUEUE_NAME, worker_concurrency=1)


def register_schedules() -> None:
    """Register DBOS schedules. Call after DBOS.launch()."""
    DBOS.create_schedule(
        schedule_name=RENEW_SCHEDULE_NAME,
        workflow_fn=renew_watch_channel,
        schedule=RENEW_SCHEDULE_CRON,
        context="cron",
    )


def seed_cursor_if_ready(drive: DriveClient | None) -> None:
    """Seed drive_change_cursor if the DriveClient is available."""
    if drive is None:
        return
    seed_cursor(drive)


def ensure_active_channel_if_ready(drive: DriveClient | None, settings: Settings) -> None:
    """Seed the first watch channel at startup if we can."""
    ensure_active_channel(drive, settings)


def enqueue_startup_backfill() -> None:
    """Enqueue the backfill workflow to run once at startup.

    Idempotent — backfill only enqueues convert_note for notes that are
    missing or whose md5 has changed, so a spurious run is a no-op.
    """
    DBOS.enqueue_workflow(POLL_QUEUE_NAME, backfill)
