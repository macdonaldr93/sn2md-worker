from __future__ import annotations

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.paths import logical_key
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import conversions
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.poll_changes import CONVERT_QUEUE_NAME

__all__ = ["backfill", "backfill_impl"]

_log = get_logger("sn2md_worker.workflows.backfill")


@DBOS.workflow()
def backfill() -> None:
    """DBOS-durable wrapper that delegates to the plain implementation."""
    backfill_impl(drive=get_drive_client(), settings=get_settings())


def backfill_impl(*, drive: DriveClient, settings: Settings) -> None:
    """Walk the source folder tree and enqueue conversions for anything stale."""
    _log.info("backfill_started")
    try:
        if not settings.drive.source_folder_id:
            _log.warning("backfill_skipped", reason="no_source_folder")
            return

        enqueued = 0
        skipped = 0
        for file, source_path in drive.list_all_notes(settings.drive.source_folder_id):
            if _needs_conversion(source_path=source_path, md5=file.md5_checksum):
                DBOS.enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, file.id, source_path)
                enqueued += 1
            else:
                skipped += 1

        _log.info("backfill_succeeded", enqueued=enqueued, skipped=skipped)
    except Exception as exc:
        _log.error("backfill_failed", error=str(exc), exc_info=True)
        raise


def _needs_conversion(*, source_path: str, md5: str | None) -> bool:
    key = logical_key(source_path)
    with sql_session() as session:
        record = conversions.get_by_logical_key(session, key)
    if record is None:
        return True
    if record.last_status != ConversionStatus.SUCCESS:
        return True
    return record.source_md5 != md5
