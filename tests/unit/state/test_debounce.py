from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from sn2md_worker.state import debounce


class TestRecordProbeForANewFileId:
    def test_creates_the_row_with_stable_since_set_to_now(self, session: Session) -> None:
        # GIVEN
        when = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

        # WHEN
        state = debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=when)
        session.flush()

        # THEN
        assert state.file_id == "file-1"
        assert state.stable_since == when


class TestRecordProbeWhenSizeAndMd5AreUnchanged:
    def test_preserves_stable_since_and_advances_updated_at(self, session: Session) -> None:
        # GIVEN
        t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        t1 = t0 + timedelta(seconds=10)
        debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t0)
        session.flush()

        # WHEN — a follow-up probe with identical size + md5
        debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t1)
        session.flush()

        # THEN
        state = debounce.get(session, "file-1")
        assert state is not None
        assert state.stable_since == t0
        assert state.updated_at == t1


class TestRecordProbeWhenSizeChanges:
    def test_resets_stable_since_to_the_new_observation(self, session: Session) -> None:
        # GIVEN
        t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        t1 = t0 + timedelta(seconds=10)
        debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t0)
        session.flush()

        # WHEN — the file's size is different
        debounce.record_probe(session, file_id="file-1", size=200, md5="abc", when=t1)
        session.flush()

        # THEN
        state = debounce.get(session, "file-1")
        assert state is not None
        assert state.stable_since == t1


class TestClear:
    def test_removes_the_row_and_returns_true_when_state_exists(self, session: Session) -> None:
        # GIVEN
        debounce.record_probe(
            session,
            file_id="file-1",
            size=100,
            md5="abc",
            when=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        )
        session.flush()

        # WHEN
        removed = debounce.clear(session, "file-1")
        session.flush()

        # THEN
        assert removed is True
        assert debounce.get(session, "file-1") is None

    def test_returns_false_when_no_state_exists(self, session: Session) -> None:
        assert debounce.clear(session, "nonexistent") is False
