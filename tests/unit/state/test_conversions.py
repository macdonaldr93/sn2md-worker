from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import ConversionStatus

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _make_upsert(**overrides: object) -> ConversionUpsert:
    base = dict(
        logical_key="Notebooks/Journal/2026-07.note",
        current_file_id="file-1",
        parent_folder_id="parent-1",
        source_name="2026-07.note",
        source_path="Notebooks/Journal/2026-07.note",
        source_md5="abc123",
        output_rel_path="Notebooks/Journal/2026-07",
        last_converted_at=NOW,
    )
    base.update(overrides)
    return ConversionUpsert(**base)  # type: ignore[arg-type]


def test_upsert_inserts_when_missing(session: Session) -> None:
    conversions.upsert(session, _make_upsert())
    session.flush()

    record = conversions.get_by_logical_key(session, "Notebooks/Journal/2026-07.note")

    assert record is not None
    assert record.current_file_id == "file-1"
    assert record.source_md5 == "abc123"
    assert record.last_status == ConversionStatus.SUCCESS
    assert record.attempts == 1


def test_upsert_updates_and_increments_attempts(session: Session) -> None:
    conversions.upsert(session, _make_upsert())
    conversions.upsert(
        session,
        _make_upsert(current_file_id="file-2", source_md5="def456"),
    )
    session.flush()

    record = conversions.get_by_logical_key(session, "Notebooks/Journal/2026-07.note")

    assert record is not None
    assert record.current_file_id == "file-2"
    assert record.source_md5 == "def456"
    assert record.attempts == 2


def test_get_by_current_file_id(session: Session) -> None:
    conversions.upsert(session, _make_upsert(current_file_id="file-9"))
    session.flush()

    record = conversions.get_by_current_file_id(session, "file-9")

    assert record is not None
    assert record.logical_key == "Notebooks/Journal/2026-07.note"


def test_get_by_current_file_id_returns_none_when_stale(session: Session) -> None:
    conversions.upsert(session, _make_upsert(current_file_id="file-new"))
    session.flush()

    assert conversions.get_by_current_file_id(session, "file-old") is None


def test_delete_by_logical_key(session: Session) -> None:
    conversions.upsert(session, _make_upsert())
    session.flush()

    removed = conversions.delete_by_logical_key(session, "Notebooks/Journal/2026-07.note")
    session.flush()

    assert removed is True
    assert conversions.get_by_logical_key(session, "Notebooks/Journal/2026-07.note") is None


def test_delete_by_logical_key_returns_false_when_missing(session: Session) -> None:
    assert conversions.delete_by_logical_key(session, "does/not/exist") is False


def test_record_failure_marks_error(session: Session) -> None:
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

    record = conversions.get_by_logical_key(session, "Notebooks/failing.note")

    assert record is not None
    assert record.last_status == ConversionStatus.ERROR
    assert record.last_error == "gemini quota exceeded"
    assert record.attempts == 1
