from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sn2md_worker.state import page_conversions
from sn2md_worker.state.page_conversions import PageConversionUpsert

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)
KEY = "Notebooks/Journal/2026-07.note"


def _upsert(session: Session, *, page_index: int, page_md5: str) -> None:
    page_conversions.upsert(
        session,
        PageConversionUpsert(
            logical_key=KEY,
            page_index=page_index,
            page_md5=page_md5,
            output_rel_path=f"page-{page_index + 1:02d}.md",
            last_converted_at=NOW,
        ),
    )


class TestUpsertOfANewPage:
    def test_inserts_the_row(self, session: Session) -> None:
        # WHEN
        _upsert(session, page_index=0, page_md5="md5-p0")
        session.flush()

        # THEN
        rows = page_conversions.list_for_note(session, KEY)
        assert [(r.page_index, r.page_md5) for r in rows] == [(0, "md5-p0")]


class TestUpsertOfAnExistingPage:
    def test_updates_the_md5_and_output_path(self, session: Session) -> None:
        # GIVEN
        _upsert(session, page_index=0, page_md5="old")
        session.flush()

        # WHEN
        _upsert(session, page_index=0, page_md5="new")
        session.flush()

        # THEN
        rows = page_conversions.list_for_note(session, KEY)
        assert rows[0].page_md5 == "new"


class TestListForNote:
    def test_returns_rows_ordered_by_page_index(self, session: Session) -> None:
        # GIVEN — insert out of order
        _upsert(session, page_index=2, page_md5="c")
        _upsert(session, page_index=0, page_md5="a")
        _upsert(session, page_index=1, page_md5="b")
        session.flush()

        # WHEN / THEN
        rows = page_conversions.list_for_note(session, KEY)
        assert [r.page_index for r in rows] == [0, 1, 2]


class TestDeletePagesAtOrBeyond:
    def test_removes_matching_rows_and_returns_count(self, session: Session) -> None:
        # GIVEN
        for i in range(4):
            _upsert(session, page_index=i, page_md5=f"md5-{i}")
        session.flush()

        # WHEN — trim to first 2 pages
        removed = page_conversions.delete_pages_at_or_beyond(session, logical_key=KEY, page_index=2)
        session.flush()

        # THEN
        assert removed == 2
        remaining = page_conversions.list_for_note(session, KEY)
        assert [r.page_index for r in remaining] == [0, 1]

    def test_returns_zero_when_no_rows_match(self, session: Session) -> None:
        # GIVEN
        _upsert(session, page_index=0, page_md5="md5-0")
        session.flush()

        # WHEN
        removed = page_conversions.delete_pages_at_or_beyond(session, logical_key=KEY, page_index=5)

        # THEN
        assert removed == 0


class TestDeleteAllForNote:
    def test_removes_every_row_for_the_note(self, session: Session) -> None:
        # GIVEN
        for i in range(3):
            _upsert(session, page_index=i, page_md5=f"md5-{i}")
        session.flush()

        # WHEN
        removed = page_conversions.delete_all_for_note(session, KEY)
        session.flush()

        # THEN
        assert removed == 3
        assert page_conversions.list_for_note(session, KEY) == []
