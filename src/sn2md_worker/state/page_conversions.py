from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from sn2md_worker.state.models import PageConversion

__all__ = [
    "PageConversionUpsert",
    "PageConversionView",
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


@dataclass(frozen=True)
class PageConversionView:
    """Read-only snapshot of a `page_conversions` row."""

    logical_key: str
    page_index: int
    page_md5: str
    output_rel_path: str
    last_converted_at: datetime


def list_for_note(session: Session, logical_key: str) -> list[PageConversionView]:
    """All page rows for a given note, ordered by index."""
    stmt = (
        select(PageConversion)
        .where(PageConversion.logical_key == logical_key)
        .order_by(PageConversion.page_index)
    )
    return [
        PageConversionView(
            logical_key=row.logical_key,
            page_index=row.page_index,
            page_md5=row.page_md5,
            output_rel_path=row.output_rel_path,
            last_converted_at=row.last_converted_at,
        )
        for row in session.execute(stmt).scalars()
    ]


def upsert(session: Session, data: PageConversionUpsert) -> None:
    """Insert-or-update a PageConversion atomically on `(logical_key, page_index)`."""
    stmt = sqlite_insert(PageConversion).values(
        logical_key=data.logical_key,
        page_index=data.page_index,
        page_md5=data.page_md5,
        output_rel_path=data.output_rel_path,
        last_converted_at=data.last_converted_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PageConversion.logical_key, PageConversion.page_index],
        set_={
            "page_md5": stmt.excluded.page_md5,
            "output_rel_path": stmt.excluded.output_rel_path,
            "last_converted_at": stmt.excluded.last_converted_at,
        },
    )
    session.execute(stmt)


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
