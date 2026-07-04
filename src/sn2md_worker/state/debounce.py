from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from sn2md_worker.state.models import DebounceState

__all__ = ["clear", "get", "record_probe"]


def get(session: Session, file_id: str) -> DebounceState | None:
    return session.get(DebounceState, file_id)


def record_probe(
    session: Session,
    *,
    file_id: str,
    size: int | None,
    md5: str | None,
    when: datetime,
) -> DebounceState:
    """Record a size/md5 observation. Preserves stable_since when unchanged."""
    state = session.get(DebounceState, file_id)
    if state is None:
        state = DebounceState(
            file_id=file_id,
            last_size=size,
            last_md5=md5,
            stable_since=when,
            updated_at=when,
        )
        session.add(state)
        return state

    unchanged = state.last_size == size and state.last_md5 == md5
    state.last_size = size
    state.last_md5 = md5
    state.updated_at = when
    if not unchanged:
        state.stable_since = when
    return state


def clear(session: Session, file_id: str) -> bool:
    state = session.get(DebounceState, file_id)
    if state is None:
        return False
    session.delete(state)
    return True
