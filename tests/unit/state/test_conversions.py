from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import ConversionStatus

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
LOGICAL_KEY = "Notebooks/Journal/2026-07.note"


def _make_upsert(**overrides: object) -> ConversionUpsert:
    base = dict(
        logical_key=LOGICAL_KEY,
        current_file_id="file-1",
        parent_folder_id="parent-1",
        source_name="2026-07.note",
        source_path=LOGICAL_KEY,
        source_md5="abc123",
        output_rel_path="Notebooks/Journal/2026-07",
        last_converted_at=NOW,
    )
    base.update(overrides)
    return ConversionUpsert(**base)  # type: ignore[arg-type]


class TestUpsertOfANewRecord:
    def test_inserts_the_record_with_attempts_one(self, session: Session) -> None:
        # WHEN
        conversions.upsert(session, _make_upsert())
        session.flush()

        # THEN
        record = conversions.get_by_logical_key(session, LOGICAL_KEY)
        assert record is not None
        assert record.current_file_id == "file-1"
        assert record.source_md5 == "abc123"
        assert record.last_status == ConversionStatus.SUCCESS
        assert record.attempts == 1


class TestUpsertOfAnExistingRecord:
    def test_updates_fields_and_increments_attempts(self, session: Session) -> None:
        # GIVEN — an existing record
        conversions.upsert(session, _make_upsert())
        session.flush()

        # WHEN — we upsert with a new file_id and md5
        conversions.upsert(session, _make_upsert(current_file_id="file-2", source_md5="def456"))
        session.flush()

        # THEN
        record = conversions.get_by_logical_key(session, LOGICAL_KEY)
        assert record is not None
        assert record.current_file_id == "file-2"
        assert record.source_md5 == "def456"
        assert record.attempts == 2


class TestGetByCurrentFileId:
    def test_returns_the_matching_record_when_the_file_id_is_active(self, session: Session) -> None:
        # GIVEN
        conversions.upsert(session, _make_upsert(current_file_id="file-9"))
        session.flush()

        # WHEN
        record = conversions.get_by_current_file_id(session, "file-9")

        # THEN
        assert record is not None
        assert record.logical_key == LOGICAL_KEY

    def test_returns_none_when_no_record_points_to_that_file_id(self, session: Session) -> None:
        # GIVEN — the stored record points to a different file_id
        conversions.upsert(session, _make_upsert(current_file_id="file-new"))
        session.flush()

        # WHEN / THEN
        assert conversions.get_by_current_file_id(session, "file-old") is None


class TestDeleteByLogicalKey:
    def test_removes_the_row_and_returns_true_when_the_record_exists(
        self, session: Session
    ) -> None:
        # GIVEN
        conversions.upsert(session, _make_upsert())
        session.flush()

        # WHEN
        removed = conversions.delete_by_logical_key(session, LOGICAL_KEY)
        session.flush()

        # THEN
        assert removed is True
        assert conversions.get_by_logical_key(session, LOGICAL_KEY) is None

    def test_returns_false_when_no_record_matches(self, session: Session) -> None:
        assert conversions.delete_by_logical_key(session, "does/not/exist") is False


class TestRecordFailure:
    def test_marks_the_record_as_error_with_the_provided_message(self, session: Session) -> None:
        # WHEN
        conversions.record_failure(
            session,
            logical_key="Notebooks/failing.note",
            current_file_id="file-x",
            source_name="failing.note",
            source_path="Notebooks/failing.note",
            error="gemini quota exceeded",
            when=NOW,
        )
        session.flush()

        # THEN
        record = conversions.get_by_logical_key(session, "Notebooks/failing.note")
        assert record is not None
        assert record.last_status == ConversionStatus.ERROR
        assert record.last_error == "gemini quota exceeded"
        assert record.attempts == 1
