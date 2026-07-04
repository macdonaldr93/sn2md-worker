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


class TestCreatingAChannel:
    def test_stores_the_channel_as_inactive(self, session: Session) -> None:
        # WHEN
        watch_channels.create(session, _make("chan-1"))
        session.flush()

        # THEN
        channels = watch_channels.list_all(session)
        assert len(channels) == 1
        assert channels[0].channel_id == "chan-1"
        assert channels[0].is_active is False


class TestMarkingAChannelActive:
    def test_promotes_the_named_channel_and_deactivates_any_others(self, session: Session) -> None:
        # GIVEN — two inactive channels
        watch_channels.create(session, _make("chan-1"))
        watch_channels.create(session, _make("chan-2"))
        session.flush()

        # WHEN
        watch_channels.mark_active(session, "chan-1")
        session.flush()

        # THEN
        active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "chan-1"

        # WHEN — a different channel is promoted
        watch_channels.mark_active(session, "chan-2")
        session.flush()

        # THEN — the previous active channel is no longer active
        active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "chan-2"


class TestGetActive:
    def test_returns_none_when_no_channel_has_been_promoted(self, session: Session) -> None:
        # GIVEN — a channel exists but none is active
        watch_channels.create(session, _make("chan-1"))
        session.flush()

        # WHEN / THEN
        assert watch_channels.get_active(session) is None
