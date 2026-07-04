from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sn2md_worker.state import cursor


class TestGetBeforeAnythingIsStored:
    def test_returns_none(self, session: Session) -> None:
        assert cursor.get(session) is None


class TestSetCursor:
    def test_creates_the_singleton_row_on_first_call(self, session: Session) -> None:
        # GIVEN
        when = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

        # WHEN
        cursor.set_cursor(session, "42", when)
        session.flush()

        # THEN
        got = cursor.get(session)
        assert got is not None
        assert got.page_token == "42"
        assert got.last_polled_at == when

    def test_updates_the_singleton_row_on_subsequent_calls(self, session: Session) -> None:
        # GIVEN — an existing cursor row
        first = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        later = datetime(2026, 7, 4, 12, 5, tzinfo=UTC)
        cursor.set_cursor(session, "42", first)
        session.flush()

        # WHEN
        cursor.set_cursor(session, "99", later)
        session.flush()

        # THEN
        got = cursor.get(session)
        assert got is not None
        assert got.page_token == "99"
        assert got.last_polled_at == later
