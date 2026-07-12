from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, DrivePermanentError, get_drive_client
from sn2md_worker.drive.models import ChangeEvent
from sn2md_worker.drive.paths import resolve_source_path
from sn2md_worker.logging import get_logger
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.state import cursor
from sn2md_worker.workflows.backfill import backfill
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output
from sn2md_worker.workflows.queues import (
    CONVERT_QUEUE_NAME,
    DELETE_QUEUE_NAME,
    POLL_QUEUE_NAME,
)

__all__ = [
    "CONVERT_QUEUE_NAME",
    "DELETE_QUEUE_NAME",
    "POLL_TRIGGER_FALLBACK",
    "poll_changes",
    "poll_changes_impl",
    "scheduled_poll_changes",
    "seed_cursor",
]

_NOTE_EXTENSION = ".note"
POLL_TRIGGER_FALLBACK = "fallback"

_log = get_logger("sn2md_worker.workflows.poll_changes")


@DBOS.workflow()
def poll_changes(trigger_source: str) -> None:
    poll_changes_impl(
        trigger_source=trigger_source,
        drive=get_drive_client(),
        settings=get_settings(),
    )


@DBOS.workflow()
def scheduled_poll_changes(scheduled_time: datetime, context: str) -> None:
    """DBOS-scheduled fallback. Enqueue a poll onto poll_queue (rather
    than running inline) so it serializes behind any webhook-triggered
    poll instead of racing it on the shared cursor. The safety net for
    push notifications Google dropped while the process stayed up."""
    DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_FALLBACK)


def poll_changes_impl(
    *,
    trigger_source: str,
    drive: DriveClient,
    settings: Settings,
) -> None:
    """Walk changes since the last saved cursor and enqueue conversions.

    Cursor advances **per page** rather than once at the end. If the walk
    dies mid-page (Drive down, DBOS unavailable), the cursor stays at
    the start of the failing page and DBOS retries pick up there instead
    of re-walking every page since the pre-crash token. Per-change
    dispatch errors are logged and skipped — a change for a since-deleted
    file that surfaces as a permanent 404 shouldn't stall every later
    change on the same page.
    """
    with structlog.contextvars.bound_contextvars(workflow="poll_changes", trigger=trigger_source):
        _log.info("poll_changes_started")
        try:
            page_token = _load_or_init_cursor(drive)
            enqueued = 0
            ignored = 0
            errored = 0

            # Cache metadata lookups for the duration of this poll run.
            # `resolve_source_path` walks a file's parent chain; sibling
            # changes on the same page usually share several ancestors, so
            # one cache pays for the whole walk. Fresh per run so a rename
            # doesn't get served a stale entry on the next poll.
            metadata_cache: dict[str, NoteMetadata] = {}

            def cached_get_metadata(fid: str) -> NoteMetadata:
                if fid not in metadata_cache:
                    metadata_cache[fid] = drive.get_metadata(fid, fields="id,name,parents")
                return metadata_cache[fid]

            while True:
                try:
                    page = drive.changes_list(page_token=page_token)
                except DrivePermanentError as exc:
                    # A 4xx on `changes.list` almost always means our
                    # persisted cursor is older than Drive's change-log
                    # window (docs: tokens more than a few weeks old can
                    # 404). Reset from `getStartPageToken` and enqueue a
                    # backfill — no work is lost because backfill walks
                    # the actual folder tree from the vault side.
                    _log.warning(
                        "poll_changes_cursor_expired",
                        cursor=page_token,
                        error=str(exc),
                    )
                    new_token = drive.get_start_page_token()
                    _save_cursor(new_token)
                    DBOS.enqueue_workflow(POLL_QUEUE_NAME, backfill)
                    _log.info(
                        "poll_changes_recovery_enqueued",
                        new_cursor=new_token,
                    )
                    return

                for change in page.changes:
                    try:
                        dispatched = _dispatch(change, cached_get_metadata, settings)
                    except DrivePermanentError as exc:
                        # 4xx from Drive: the file is gone / rejected / forbidden.
                        # No retry can fix that, so log it and keep processing
                        # the rest of the page. Transient errors and any other
                        # exception (DBOS enqueue failure, bug) propagate and
                        # stall the workflow so DBOS retries from the last
                        # saved cursor.
                        _log.warning(
                            "poll_changes_dispatch_failed",
                            file_id=change.file_id,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        errored += 1
                        continue
                    if dispatched:
                        enqueued += 1
                    else:
                        ignored += 1

                if page.next_page_token:
                    page_token = page.next_page_token
                    _save_cursor(page_token)
                    continue
                if page.new_start_page_token:
                    page_token = page.new_start_page_token
                    _save_cursor(page_token)
                break

            _log.info(
                "poll_changes_succeeded",
                enqueued=enqueued,
                ignored=ignored,
                errored=errored,
                cursor=page_token,
            )
        except Exception as exc:
            _log.error("poll_changes_failed", error=str(exc), exc_info=True)
            raise


def seed_cursor(drive: DriveClient) -> None:
    """Ensure `drive_change_cursor` has a value; safe to call every boot."""
    with sql_session() as session:
        if cursor.get(session) is not None:
            return
    token = drive.get_start_page_token()
    _save_cursor(token)
    _log.info("cursor_seeded", token=token)


def _dispatch(
    change: ChangeEvent,
    get_metadata: Callable[[str], NoteMetadata],
    settings: Settings,
) -> bool:
    """Enqueue convert_note (or delete_output) for eligible changes."""
    if change.removed:
        DBOS.enqueue_workflow(DELETE_QUEUE_NAME, delete_output, change.file_id)
        _log.info(
            "poll_changes_enqueued",
            file_id=change.file_id,
            target="delete_output",
            reason="removed",
        )
        return True

    if change.file is None:
        return False
    if change.file.trashed:
        DBOS.enqueue_workflow(DELETE_QUEUE_NAME, delete_output, change.file_id)
        _log.info(
            "poll_changes_enqueued",
            file_id=change.file_id,
            target="delete_output",
            reason="trashed",
        )
        return True
    if not change.file.name.lower().endswith(_NOTE_EXTENSION):
        return False

    source_path = resolve_source_path(
        file_id=change.file_id,
        root_folder_id=settings.drive.source_folder_id,
        get_metadata=get_metadata,
    )
    if source_path is None:
        return False

    DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, change.file_id, source_path)
    _log.info(
        "poll_changes_enqueued",
        file_id=change.file_id,
        target="convert_note",
        source_path=source_path,
    )
    return True


def _load_or_init_cursor(drive: DriveClient) -> str:
    with sql_session() as session:
        record = cursor.get(session)
        if record is not None:
            return record.page_token
    token = drive.get_start_page_token()
    _save_cursor(token)
    return token


def _save_cursor(page_token: str) -> None:
    with sql_session() as session, session.begin():
        cursor.set_cursor(session, page_token, datetime.now(UTC))
