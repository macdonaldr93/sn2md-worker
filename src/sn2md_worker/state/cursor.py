from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from sn2md_worker.state.models import DriveChangeCursor

__all__ = ["CursorView", "get", "set_cursor"]

_SINGLETON_ID = 1


@dataclass(frozen=True)
class CursorView:
    """Read-only snapshot of the `drive_change_cursor` singleton."""

    page_token: str
    last_polled_at: datetime


def get(session: Session) -> CursorView | None:
    record = session.get(DriveChangeCursor, _SINGLETON_ID)
    if record is None:
        return None
    return CursorView(page_token=record.page_token, last_polled_at=record.last_polled_at)


def set_cursor(session: Session, page_token: str, when: datetime) -> None:
    """Insert-or-update the singleton cursor atomically."""
    stmt = sqlite_insert(DriveChangeCursor).values(
        id=_SINGLETON_ID,
        page_token=page_token,
        last_polled_at=when,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[DriveChangeCursor.id],
        set_={
            "page_token": stmt.excluded.page_token,
            "last_polled_at": stmt.excluded.last_polled_at,
        },
    )
    session.execute(stmt)
