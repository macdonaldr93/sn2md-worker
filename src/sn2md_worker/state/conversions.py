from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from sn2md_worker.state.models import ConversionRecord, ConversionStatus

__all__ = [
    "ConversionRecordView",
    "ConversionUpsert",
    "delete_by_logical_key",
    "get_by_current_file_id",
    "get_by_logical_key",
    "list_all_by_key",
    "list_recent_by_status",
    "record_failure",
    "set_current_file_id",
    "upsert",
]


@dataclass(frozen=True)
class ConversionUpsert:
    logical_key: str
    current_file_id: str
    parent_folder_id: str | None
    source_name: str
    source_path: str
    source_md5: str | None
    output_rel_path: str
    last_converted_at: datetime
    status: str = ConversionStatus.SUCCESS
    last_error: str | None = None


@dataclass(frozen=True)
class ConversionRecordView:
    """Read-only snapshot of a `conversion_records` row.

    Repos return this so callers never hold a live ORM object across a
    session boundary — no risk of `DetachedInstanceError` if we ever add
    a lazy relationship, no ambiguity about which fields are already
    loaded. All fields mirror `ConversionRecord`.
    """

    logical_key: str
    current_file_id: str
    parent_folder_id: str | None
    source_name: str
    source_path: str
    source_md5: str | None
    output_rel_path: str
    last_status: str
    last_converted_at: datetime
    attempts: int
    last_error: str | None


def upsert(session: Session, data: ConversionUpsert) -> None:
    """Insert-or-update a ConversionRecord atomically on `logical_key`.

    Uses SQLite `INSERT ... ON CONFLICT DO UPDATE` so two concurrent
    writers observing the same missing row cannot both insert. On
    conflict, all mutable fields are overwritten and `attempts` is
    incremented by one.
    """
    stmt = sqlite_insert(ConversionRecord).values(
        logical_key=data.logical_key,
        current_file_id=data.current_file_id,
        parent_folder_id=data.parent_folder_id,
        source_name=data.source_name,
        source_path=data.source_path,
        source_md5=data.source_md5,
        output_rel_path=data.output_rel_path,
        last_status=data.status,
        last_converted_at=data.last_converted_at,
        attempts=1,
        last_error=data.last_error,
    )
    row = ConversionRecord.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=[ConversionRecord.logical_key],
        set_={
            "current_file_id": stmt.excluded.current_file_id,
            "parent_folder_id": stmt.excluded.parent_folder_id,
            "source_name": stmt.excluded.source_name,
            "source_path": stmt.excluded.source_path,
            "source_md5": stmt.excluded.source_md5,
            "output_rel_path": stmt.excluded.output_rel_path,
            "last_status": stmt.excluded.last_status,
            "last_converted_at": stmt.excluded.last_converted_at,
            "attempts": row.attempts + 1,
            "last_error": stmt.excluded.last_error,
        },
    )
    session.execute(stmt)


def record_failure(
    session: Session,
    *,
    logical_key: str,
    current_file_id: str,
    source_name: str,
    source_path: str,
    error: str,
    when: datetime,
) -> None:
    """Mark a logical note as ERROR without touching successful-conversion fields.

    Atomic on `logical_key`. On conflict, preserves `parent_folder_id`,
    `source_md5`, `output_rel_path`, and `last_converted_at` (all
    populated by prior successful runs); only updates identity/error
    fields and increments `attempts`.
    """
    stmt = sqlite_insert(ConversionRecord).values(
        logical_key=logical_key,
        current_file_id=current_file_id,
        parent_folder_id=None,
        source_name=source_name,
        source_path=source_path,
        source_md5=None,
        output_rel_path="",
        last_status=ConversionStatus.ERROR,
        last_converted_at=when,
        attempts=1,
        last_error=error,
    )
    row = ConversionRecord.__table__.c
    stmt = stmt.on_conflict_do_update(
        index_elements=[ConversionRecord.logical_key],
        set_={
            "current_file_id": stmt.excluded.current_file_id,
            "source_name": stmt.excluded.source_name,
            "source_path": stmt.excluded.source_path,
            "last_status": stmt.excluded.last_status,
            "attempts": row.attempts + 1,
            "last_error": stmt.excluded.last_error,
        },
    )
    session.execute(stmt)


def set_current_file_id(session: Session, *, logical_key: str, new_file_id: str) -> None:
    """Repoint an existing conversion record to a new Drive file id.

    Handles Supernote's replace-then-delete pattern: a stale file id
    lingers in our record but a live file exists at the same logical
    path. No-op if the row doesn't exist.
    """
    session.execute(
        update(ConversionRecord)
        .where(ConversionRecord.logical_key == logical_key)
        .values(current_file_id=new_file_id)
    )


def get_by_logical_key(session: Session, logical_key: str) -> ConversionRecordView | None:
    record = session.get(ConversionRecord, logical_key)
    return _to_view(record) if record is not None else None


def get_by_current_file_id(session: Session, file_id: str) -> ConversionRecordView | None:
    stmt = select(ConversionRecord).where(ConversionRecord.current_file_id == file_id)
    record = session.execute(stmt).scalar_one_or_none()
    return _to_view(record) if record is not None else None


def list_all_by_key(session: Session) -> dict[str, ConversionRecordView]:
    """Return every conversion record indexed by `logical_key`.

    Bulk load for callers that would otherwise do N+1 per-file lookups.
    For a Supernote-sized vault (hundreds to low thousands of notes),
    the whole set fits comfortably in memory.
    """
    stmt = select(ConversionRecord)
    return {row.logical_key: _to_view(row) for row in session.execute(stmt).scalars()}


def list_recent_by_status(
    session: Session, *, status: str, limit: int = 20
) -> list[ConversionRecordView]:
    """Recent conversion records filtered by last_status, newest first."""
    stmt = (
        select(ConversionRecord)
        .where(ConversionRecord.last_status == status)
        .order_by(ConversionRecord.last_converted_at.desc())
        .limit(limit)
    )
    return [_to_view(row) for row in session.execute(stmt).scalars()]


def delete_by_logical_key(session: Session, logical_key: str) -> bool:
    record = session.get(ConversionRecord, logical_key)
    if record is None:
        return False
    session.delete(record)
    return True


def _to_view(record: ConversionRecord) -> ConversionRecordView:
    return ConversionRecordView(
        logical_key=record.logical_key,
        current_file_id=record.current_file_id,
        parent_folder_id=record.parent_folder_id,
        source_name=record.source_name,
        source_path=record.source_path,
        source_md5=record.source_md5,
        output_rel_path=record.output_rel_path,
        last_status=record.last_status,
        last_converted_at=record.last_converted_at,
        attempts=record.attempts,
        last_error=record.last_error,
    )
