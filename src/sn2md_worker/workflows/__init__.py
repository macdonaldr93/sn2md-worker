"""DBOS workflows.

Importing this package registers workflow decorators with DBOS. The
entrypoint separately calls the register_/ensure_ helpers below in the
correct order after `DBOS.launch()`.
"""

from __future__ import annotations

from collections.abc import Callable

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.correlation import new_correlation_id
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.logging import get_logger
from sn2md_worker.workflows.backfill import backfill, backfill_impl, scheduled_backfill
from sn2md_worker.workflows.convert_note import convert_note, convert_note_impl
from sn2md_worker.workflows.delete_output import delete_output, delete_output_impl
from sn2md_worker.workflows.poll_changes import (
    poll_changes,
    poll_changes_impl,
    scheduled_poll_changes,
    seed_cursor,
)
from sn2md_worker.workflows.queues import (
    CONVERT_QUEUE_NAME,
    DELETE_QUEUE_NAME,
    POLL_QUEUE_NAME,
)
from sn2md_worker.workflows.renew_watch import (
    ensure_active_channel,
    renew_watch_channel,
    renew_watch_channel_impl,
)

_log = get_logger("sn2md_worker.workflows")

RENEW_SCHEDULE_NAME = "renew-watch-channel"
RENEW_SCHEDULE_CRON = "0 6 * * *"  # daily at 06:00 UTC
FALLBACK_POLL_SCHEDULE_NAME = "fallback-poll-changes"
BACKFILL_SWEEP_SCHEDULE_NAME = "backfill-sweep"

__all__ = [
    "BACKFILL_SWEEP_SCHEDULE_NAME",
    "CONVERT_QUEUE_NAME",
    "DELETE_QUEUE_NAME",
    "FALLBACK_POLL_SCHEDULE_NAME",
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
    "scheduled_backfill",
    "scheduled_poll_changes",
    "seed_cursor",
    "seed_cursor_if_ready",
]


def register_queues() -> None:
    """Register DBOS queues. Call after DBOS.launch().

    `delete_queue` is separate from `convert_queue` so a batch of long
    Gemini-bound conversions never blocks the fast filesystem-only
    deletes; a stale delete arriving mid-backfill can complete without
    waiting behind the pipeline in front of it.
    """
    settings = get_settings()
    DBOS.register_queue(
        CONVERT_QUEUE_NAME,
        worker_concurrency=settings.queue.convert_concurrency,
    )
    DBOS.register_queue(DELETE_QUEUE_NAME, worker_concurrency=2)
    DBOS.register_queue(POLL_QUEUE_NAME, worker_concurrency=1)


def register_schedules() -> None:
    """Register DBOS schedules. Call after DBOS.launch().

    Idempotent — DBOS persists schedule rows in `workflow_schedules`, so
    a second boot on the same DB already has our rows. We pre-check via
    `DBOS.get_schedule` rather than catching a substring on `DBOSException`
    so a DBOS message-format change doesn't turn benign re-registration
    into a boot failure. Changing a cron requires deleting the row
    (or nuking the SQLite file).
    """
    settings = get_settings()
    _register_schedule(RENEW_SCHEDULE_NAME, renew_watch_channel, RENEW_SCHEDULE_CRON)
    _register_schedule(
        FALLBACK_POLL_SCHEDULE_NAME,
        scheduled_poll_changes,
        settings.drive.fallback_poll_cron,
    )
    _register_schedule(
        BACKFILL_SWEEP_SCHEDULE_NAME,
        scheduled_backfill,
        settings.drive.backfill_sweep_cron,
    )


def _register_schedule(schedule_name: str, workflow_fn: Callable[..., None], schedule: str) -> None:
    if DBOS.get_schedule(schedule_name) is not None:
        _log.info("schedule_already_registered", schedule_name=schedule_name)
        return
    DBOS.create_schedule(
        schedule_name=schedule_name,
        workflow_fn=workflow_fn,
        schedule=schedule,
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
    Root trigger: mints the correlation id the backfill (and every
    convert_note it fans out to) will carry, and logs it so boot logs
    link to the run.
    """
    correlation_id = new_correlation_id()
    DBOS.enqueue_workflow(POLL_QUEUE_NAME, backfill, correlation_id)
    _log.info("startup_backfill_enqueued", correlation_id=correlation_id)
