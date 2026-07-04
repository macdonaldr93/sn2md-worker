from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from sn2md_worker.state import watch_channels
from sn2md_worker.state.watch_channels import NewWatchChannel


def _make(channel_id: str, **overrides: object) -> NewWatchChannel:
    now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    base = dict(
        channel_id=channel_id,
        resource_id=f"res-{channel_id}",
        token="secret",
        expires_at=now + timedelta(days=7),
        start_page_token="10",
        created_at=now,
    )
    base.update(overrides)
    return NewWatchChannel(**base)  # type: ignore[arg-type]


def test_create_stores_channel_as_inactive(session: Session) -> None:
    watch_channels.create(session, _make("chan-1"))
    session.flush()

    channels = watch_channels.list_all(session)

    assert len(channels) == 1
    assert channels[0].channel_id == "chan-1"
    assert channels[0].is_active is False


def test_mark_active_promotes_one_and_deactivates_others(session: Session) -> None:
    watch_channels.create(session, _make("chan-1"))
    watch_channels.create(session, _make("chan-2"))
    session.flush()

    watch_channels.mark_active(session, "chan-1")
    session.flush()

    active = watch_channels.get_active(session)

    assert active is not None
    assert active.channel_id == "chan-1"

    watch_channels.mark_active(session, "chan-2")
    session.flush()

    active = watch_channels.get_active(session)

    assert active is not None
    assert active.channel_id == "chan-2"


def test_get_active_returns_none_when_no_active_channel(session: Session) -> None:
    watch_channels.create(session, _make("chan-1"))
    session.flush()

    assert watch_channels.get_active(session) is None
