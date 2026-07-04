from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sn2md_worker.state import cursor


def test_get_returns_none_initially(session: Session) -> None:
    assert cursor.get(session) is None


def test_set_creates_then_updates(session: Session) -> None:
    first = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    later = datetime(2026, 7, 4, 12, 5, tzinfo=UTC)

    cursor.set_cursor(session, "42", first)
    session.flush()

    got = cursor.get(session)
    assert got is not None
    assert got.page_token == "42"
    assert got.last_polled_at == first

    cursor.set_cursor(session, "99", later)
    session.flush()

    got = cursor.get(session)
    assert got is not None
    assert got.page_token == "99"
    assert got.last_polled_at == later
