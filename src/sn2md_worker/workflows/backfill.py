from __future__ import annotations

from datetime import datetime

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.paths import UnsafePathError, logical_key
from sn2md_worker.correlation import new_correlation_id
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.sources.protocol import NoteSource
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionRecordView
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.queues import CONVERT_QUEUE_NAME, POLL_QUEUE_NAME

__all__ = ["backfill", "backfill_impl", "scheduled_backfill"]

_log = get_logger("sn2md_worker.workflows.backfill")


@DBOS.workflow()
def backfill(correlation_id: str | None = None) -> None:
    with structlog.contextvars.bound_contextvars(dbos_workflow_id=DBOS.workflow_id):
        backfill_impl(
            source=get_drive_client(),
            settings=get_settings(),
            correlation_id=correlation_id,
        )


@DBOS.workflow()
def scheduled_backfill(scheduled_time: datetime, context: str) -> None:
    """DBOS-scheduled daily sweep. Enqueue a backfill onto poll_queue
    (rather than running inline) so it serializes behind any poll or
    prior backfill instead of racing them. The safety net for
    conversions that failed permanently (for example Gemini throttle
    retries exhausted): nothing else re-drives those while the process
    stays up, so the sweep bounds their staleness to one day.

    Signature stays exactly `(scheduled_time, context)`: that is what
    `DBOS.create_schedule` invokes scheduled workflows with. Root
    trigger: a fresh correlation id is minted per cron tick."""
    correlation_id = new_correlation_id()
    DBOS.enqueue_workflow(POLL_QUEUE_NAME, backfill, correlation_id)
    _log.info("scheduled_backfill_enqueued", correlation_id=correlation_id)


def backfill_impl(
    *,
    source: NoteSource,
    settings: Settings,
    correlation_id: str | None = None,
) -> None:
    """Walk the source folder tree and enqueue conversions for anything stale.

    Loads every conversion record up front (single SELECT) instead of
    hitting SQLite once per source file. Comfortable for a Supernote-sized
    vault; if we ever grow into the tens of thousands of notes, switch
    to chunked-by-parent-folder loading.
    """
    # Self-healing mint: replays of pre-upgrade enqueues (and direct
    # invocations) arrive with None and still get a usable id.
    correlation_id = correlation_id or new_correlation_id()
    with structlog.contextvars.bound_contextvars(
        workflow="backfill", correlation_id=correlation_id
    ):
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
            for listed in source.list_all_notes(settings.drive.source_folder_id):
                file = listed.metadata
                source_path = listed.source_path
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
                    DBOS.enqueue_workflow(
                        CONVERT_QUEUE_NAME, convert_note, file.id, source_path, correlation_id
                    )
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
