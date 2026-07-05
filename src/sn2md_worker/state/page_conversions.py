from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from sn2md_worker.state.models import PageConversion

__all__ = [
    "PageConversionUpsert",
    "delete_all_for_note",
    "delete_pages_at_or_beyond",
    "list_for_note",
    "upsert",
]


@dataclass(frozen=True)
class PageConversionUpsert:
    logical_key: str
    page_index: int
    page_md5: str
    output_rel_path: str
    last_converted_at: datetime


def list_for_note(session: Session, logical_key: str) -> list[PageConversion]:
    """All page rows for a given note, ordered by index."""
    stmt = (
        select(PageConversion)
        .where(PageConversion.logical_key == logical_key)
        .order_by(PageConversion.page_index)
    )
    return list(session.execute(stmt).scalars())


def upsert(session: Session, data: PageConversionUpsert) -> PageConversion:
    record = session.get(PageConversion, (data.logical_key, data.page_index))
    if record is None:
        record = PageConversion(
            logical_key=data.logical_key,
            page_index=data.page_index,
            page_md5=data.page_md5,
            output_rel_path=data.output_rel_path,
            last_converted_at=data.last_converted_at,
        )
        session.add(record)
        return record

    record.page_md5 = data.page_md5
    record.output_rel_path = data.output_rel_path
    record.last_converted_at = data.last_converted_at
    return record


def delete_pages_at_or_beyond(session: Session, *, logical_key: str, page_index: int) -> int:
    """Drop any page rows for `logical_key` whose index is >= `page_index`.

    Used to prune state when a note has fewer pages than before.
    Returns the number of rows removed.
    """
    stmt = delete(PageConversion).where(
        PageConversion.logical_key == logical_key,
        PageConversion.page_index >= page_index,
    )
    return _rowcount(session.execute(stmt))


def delete_all_for_note(session: Session, logical_key: str) -> int:
    """Remove all page rows for a logical key. Used by `delete_output`."""
    stmt = delete(PageConversion).where(PageConversion.logical_key == logical_key)
    return _rowcount(session.execute(stmt))


def _rowcount(result: object) -> int:
    """SQLAlchemy's `CursorResult` exposes rowcount at runtime; the typed
    `Result` interface doesn't. Get it via getattr so mypy stays happy."""
    return int(getattr(result, "rowcount", 0) or 0)
