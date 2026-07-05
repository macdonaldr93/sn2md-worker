from __future__ import annotations

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.paths import UnsafePathError, logical_key
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionRecordView
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.queues import CONVERT_QUEUE_NAME

__all__ = ["backfill", "backfill_impl"]

_log = get_logger("sn2md_worker.workflows.backfill")


@DBOS.workflow()
def backfill() -> None:
    backfill_impl(drive=get_drive_client(), settings=get_settings())


def backfill_impl(*, drive: DriveClient, settings: Settings) -> None:
    """Walk the source folder tree and enqueue conversions for anything stale.

    Loads every conversion record up front (single SELECT) instead of
    hitting SQLite once per Drive file. Comfortable for a Supernote-sized
    vault; if we ever grow into the tens of thousands of notes, switch
    to chunked-by-parent-folder loading.
    """
    with structlog.contextvars.bound_contextvars(workflow="backfill"):
        _log.info("backfill_started")
        try:
            if not settings.drive.source_folder_id:
                _log.warning("backfill_skipped", reason="no_source_folder")
                return

            with sql_session() as session:
                existing = conversions.list_all_by_key(session)

            enqueued = 0
            skipped = 0
            unsafe = 0
            for file, source_path in drive.list_all_notes(settings.drive.source_folder_id):
                try:
                    key = logical_key(source_path)
                except UnsafePathError as exc:
                    # One bad name shouldn't abort the whole backfill.
                    _log.warning(
                        "backfill_skipped_unsafe_path",
                        source_path=source_path,
                        file_id=file.id,
                        error=str(exc),
                    )
                    unsafe += 1
                    continue

                if _needs_conversion(existing.get(key), file.md5_checksum):
                    DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, file.id, source_path)
                    enqueued += 1
                else:
                    skipped += 1

            _log.info("backfill_succeeded", enqueued=enqueued, skipped=skipped, unsafe=unsafe)
        except Exception as exc:
            _log.error("backfill_failed", error=str(exc), exc_info=True)
            raise


def _needs_conversion(record: ConversionRecordView | None, md5: str | None) -> bool:
    if record is None:
        return True
    if record.last_status != ConversionStatus.SUCCESS:
        return True
    return record.source_md5 != md5
