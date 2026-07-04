from __future__ import annotations

from datetime import UTC, datetime

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.drive.models import ChangeEvent
from sn2md_worker.drive.paths import resolve_source_path
from sn2md_worker.logging import get_logger
from sn2md_worker.state import cursor
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output

__all__ = ["CONVERT_QUEUE_NAME", "poll_changes", "poll_changes_impl", "seed_cursor"]

CONVERT_QUEUE_NAME = "convert_queue"
_NOTE_EXTENSION = ".note"

_log = get_logger("sn2md_worker.workflows.poll_changes")


@DBOS.workflow()
def poll_changes(trigger_source: str) -> None:
    """DBOS-durable wrapper that delegates to the plain implementation."""
    poll_changes_impl(
        trigger_source=trigger_source,
        drive=get_drive_client(),
        settings=get_settings(),
    )


def poll_changes_impl(
    *,
    trigger_source: str,
    drive: DriveClient,
    settings: Settings,
) -> None:
    """Walk changes since the last saved cursor and enqueue conversions."""
    page_token = _load_or_init_cursor(drive)
    enqueued = 0
    ignored = 0

    while True:
        page = drive.changes_list(page_token=page_token)
        for change in page.changes:
            if _dispatch(change, drive, settings):
                enqueued += 1
            else:
                ignored += 1
        if page.next_page_token:
            page_token = page.next_page_token
            continue
        if page.new_start_page_token:
            page_token = page.new_start_page_token
        break

    _save_cursor(page_token)
    _log.info(
        "poll_changes_complete",
        trigger=trigger_source,
        enqueued=enqueued,
        ignored=ignored,
        cursor=page_token,
    )


def seed_cursor(drive: DriveClient) -> None:
    """Ensure `drive_change_cursor` has a value; safe to call every boot."""
    with sql_session() as session:
        if cursor.get(session) is not None:
            return
    token = drive.get_start_page_token()
    _save_cursor(token)
    _log.info("cursor_seeded", token=token)


def _dispatch(change: ChangeEvent, drive: DriveClient, settings: Settings) -> bool:
    """Enqueue convert_note (or delete_output) for eligible changes."""
    if change.removed:
        DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, delete_output, change.file_id)
        _log.info("poll_changes_enqueued_delete", file_id=change.file_id)
        return True

    if change.file is None:
        return False
    if change.file.trashed:
        DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, delete_output, change.file_id)
        _log.info("poll_changes_enqueued_delete_trashed", file_id=change.file_id)
        return True
    if not change.file.name.lower().endswith(_NOTE_EXTENSION):
        return False

    source_path = resolve_source_path(
        file_id=change.file_id,
        root_folder_id=settings.drive.source_folder_id,
        get_metadata=lambda fid: drive.get_metadata(fid, fields="id,name,parents"),
    )
    if source_path is None:
        return False

    DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, change.file_id, source_path)
    _log.info("poll_changes_enqueued", file_id=change.file_id, source_path=source_path)
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
