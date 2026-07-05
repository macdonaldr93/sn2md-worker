from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from sn2md_worker.state.models import DebounceState

__all__ = ["DebounceView", "clear", "get", "record_probe"]


@dataclass(frozen=True)
class DebounceView:
    """Read-only snapshot of a `debounce_state` row."""

    file_id: str
    last_size: int | None
    last_md5: str | None
    stable_since: datetime | None
    updated_at: datetime


def get(session: Session, file_id: str) -> DebounceView | None:
    state = session.get(DebounceState, file_id)
    if state is None:
        return None
    return DebounceView(
        file_id=state.file_id,
        last_size=state.last_size,
        last_md5=state.last_md5,
        stable_since=state.stable_since,
        updated_at=state.updated_at,
    )


def record_probe(
    session: Session,
    *,
    file_id: str,
    size: int | None,
    md5: str | None,
    when: datetime,
) -> None:
    """Record a size/md5 observation atomically. Preserves `stable_since`
    when both `size` and `md5` are unchanged; otherwise resets it to `when`.

    Uses SQLite `IS` for NULL-safe equality — both `last_size` and
    `last_md5` are nullable, and `NULL = NULL` in SQL is `NULL` (falsy),
    which would otherwise mis-classify a NULL→NULL probe as changed.
    """
    stmt = sqlite_insert(DebounceState).values(
        file_id=file_id,
        last_size=size,
        last_md5=md5,
        stable_since=when,
        updated_at=when,
    )
    row = DebounceState.__table__.c
    same_size = row.last_size.op("IS")(stmt.excluded.last_size)
    same_md5 = row.last_md5.op("IS")(stmt.excluded.last_md5)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DebounceState.file_id],
        set_={
            "last_size": stmt.excluded.last_size,
            "last_md5": stmt.excluded.last_md5,
            "updated_at": stmt.excluded.updated_at,
            "stable_since": case(
                (same_size & same_md5, row.stable_since),
                else_=stmt.excluded.updated_at,
            ),
        },
    )
    session.execute(stmt)


def clear(session: Session, file_id: str) -> bool:
    state = session.get(DebounceState, file_id)
    if state is None:
        return False
    session.delete(state)
    return True
