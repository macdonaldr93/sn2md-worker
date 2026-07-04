from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from sn2md_worker.state.models import DriveChangeCursor

__all__ = ["get", "set_cursor"]

_SINGLETON_ID = 1


def get(session: Session) -> DriveChangeCursor | None:
    return session.get(DriveChangeCursor, _SINGLETON_ID)


def set_cursor(session: Session, page_token: str, when: datetime) -> DriveChangeCursor:
    cursor = session.get(DriveChangeCursor, _SINGLETON_ID)
    if cursor is None:
        cursor = DriveChangeCursor(id=_SINGLETON_ID, page_token=page_token, last_polled_at=when)
        session.add(cursor)
        return cursor
    cursor.page_token = page_token
    cursor.last_polled_at = when
    return cursor
