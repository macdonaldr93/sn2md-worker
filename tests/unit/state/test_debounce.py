from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from sn2md_worker.state import debounce


def test_record_probe_creates_when_missing(session: Session) -> None:
    when = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)

    state = debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=when)
    session.flush()

    assert state.file_id == "file-1"
    assert state.stable_since == when


def test_record_probe_preserves_stable_since_when_unchanged(session: Session) -> None:
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=10)

    debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t0)
    session.flush()
    debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t1)
    session.flush()

    state = debounce.get(session, "file-1")

    assert state is not None
    assert state.stable_since == t0
    assert state.updated_at == t1


def test_record_probe_resets_stable_when_size_changes(session: Session) -> None:
    t0 = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=10)

    debounce.record_probe(session, file_id="file-1", size=100, md5="abc", when=t0)
    session.flush()
    debounce.record_probe(session, file_id="file-1", size=200, md5="abc", when=t1)
    session.flush()

    state = debounce.get(session, "file-1")

    assert state is not None
    assert state.stable_since == t1


def test_clear_removes_state(session: Session) -> None:
    debounce.record_probe(
        session,
        file_id="file-1",
        size=100,
        md5="abc",
        when=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    session.flush()

    assert debounce.clear(session, "file-1") is True
    session.flush()
    assert debounce.get(session, "file-1") is None


def test_clear_returns_false_when_absent(session: Session) -> None:
    assert debounce.clear(session, "nonexistent") is False
