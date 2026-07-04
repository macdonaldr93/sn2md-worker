from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from sn2md_worker.state.models import ConversionRecord, ConversionStatus

__all__ = [
    "ConversionUpsert",
    "delete_by_logical_key",
    "get_by_current_file_id",
    "get_by_logical_key",
    "record_failure",
    "upsert",
]


@dataclass(frozen=True)
class ConversionUpsert:
    logical_key: str
    current_file_id: str
    source_name: str
    source_path: str
    source_md5: str | None
    output_rel_path: str
    last_converted_at: datetime
    status: str = ConversionStatus.SUCCESS
    last_error: str | None = None


def upsert(session: Session, data: ConversionUpsert) -> ConversionRecord:
    record = session.get(ConversionRecord, data.logical_key)
    if record is None:
        record = ConversionRecord(
            logical_key=data.logical_key,
            current_file_id=data.current_file_id,
            source_name=data.source_name,
            source_path=data.source_path,
            source_md5=data.source_md5,
            output_rel_path=data.output_rel_path,
            last_status=data.status,
            last_converted_at=data.last_converted_at,
            attempts=1,
            last_error=data.last_error,
        )
        session.add(record)
        return record

    record.current_file_id = data.current_file_id
    record.source_name = data.source_name
    record.source_path = data.source_path
    record.source_md5 = data.source_md5
    record.output_rel_path = data.output_rel_path
    record.last_status = data.status
    record.last_converted_at = data.last_converted_at
    record.attempts = (record.attempts or 0) + 1
    record.last_error = data.last_error
    return record


def get_by_logical_key(session: Session, logical_key: str) -> ConversionRecord | None:
    return session.get(ConversionRecord, logical_key)


def get_by_current_file_id(session: Session, file_id: str) -> ConversionRecord | None:
    stmt = select(ConversionRecord).where(ConversionRecord.current_file_id == file_id)
    return session.execute(stmt).scalar_one_or_none()


def delete_by_logical_key(session: Session, logical_key: str) -> bool:
    record = session.get(ConversionRecord, logical_key)
    if record is None:
        return False
    session.delete(record)
    return True


def record_failure(
    session: Session,
    *,
    logical_key: str,
    current_file_id: str,
    source_name: str,
    source_path: str,
    error: str,
    when: datetime,
) -> ConversionRecord:
    """Mark a logical note as ERROR without touching successful-conversion fields."""
    record = session.get(ConversionRecord, logical_key)
    if record is None:
        record = ConversionRecord(
            logical_key=logical_key,
            current_file_id=current_file_id,
            source_name=source_name,
            source_path=source_path,
            source_md5=None,
            output_rel_path="",
            last_status=ConversionStatus.ERROR,
            last_converted_at=when,
            attempts=1,
            last_error=error,
        )
        session.add(record)
        return record

    record.current_file_id = current_file_id
    record.source_name = source_name
    record.source_path = source_path
    record.last_status = ConversionStatus.ERROR
    record.attempts = (record.attempts or 0) + 1
    record.last_error = error
    return record
