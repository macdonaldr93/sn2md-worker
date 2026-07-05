"""BDD-style tests for renew_watch_channel_impl."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import DriveConfig, Settings, WebhookConfig
from sn2md_worker.db import set_engine
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.drive.models import ChannelInfo
from sn2md_worker.state import cursor, watch_channels
from sn2md_worker.state.models import Base
from sn2md_worker.state.watch_channels import NewWatchChannel
from sn2md_worker.workflows.renew_watch import (
    RENEWAL_HEADROOM,
    ensure_active_channel,
    renew_watch_channel_impl,
)

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'renew.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        drive=DriveConfig(source_folder_id="SRC", watch_channel_ttl_days=6),
        webhook=WebhookConfig(url="https://sn2md.example.com/webhooks/drive"),
    )


@pytest.fixture
def drive() -> MagicMock:
    m = MagicMock(spec=DriveClient)
    m.get_start_page_token.return_value = "SPT-1"
    m.watch_changes.return_value = ChannelInfo(
        id="new-channel",
        resource_id="res-1",
        expiration=NOW + timedelta(days=7),
        token="server-token",
    )
    return m


class TestWhenNoChannelExists:
    def test_creates_and_activates_a_new_channel(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — no active channel, no cursor

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — a channel was created via Drive and marked active
        drive.watch_changes.assert_called_once()
        kwargs = drive.watch_changes.call_args.kwargs
        assert kwargs["webhook_url"] == settings.webhook.url
        assert kwargs["start_page_token"] == "SPT-1"
        assert kwargs["ttl_seconds"] == 6 * 86_400

        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"
        assert active.token == "server-token"

    def test_seeds_the_cursor_when_missing(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — cursor is now the value returned by get_start_page_token
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "SPT-1"


class TestWhenActiveChannelIsExpiringSoon:
    def test_creates_a_new_channel(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — an active channel expiring in 12 hours (< headroom)
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="stale",
                    resource_id="res-stale",
                    token="stale-token",
                    webhook_url=settings.webhook.url,
                    expires_at=NOW + timedelta(hours=12),
                    start_page_token="SPT-OLD",
                    created_at=NOW - timedelta(days=6),
                ),
            )
            watch_channels.mark_active(session, "stale")

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — a new channel is created and is now active
        drive.watch_changes.assert_called_once()
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"


class TestWhenActiveChannelIsFresh:
    def test_no_new_channel_created(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — an active channel with plenty of TTL remaining
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="fresh",
                    resource_id="res-fresh",
                    token="fresh-token",
                    webhook_url=settings.webhook.url,
                    expires_at=NOW + timedelta(days=5),
                    start_page_token="SPT-CUR",
                    created_at=NOW - timedelta(days=1),
                ),
            )
            watch_channels.mark_active(session, "fresh")

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN
        drive.watch_changes.assert_not_called()
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "fresh"


class TestWhenActiveChannelHasStaleWebhookUrl:
    def test_stops_the_old_channel_and_creates_a_new_one(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — an active channel registered with a DIFFERENT webhook URL,
        # otherwise still well within its TTL.
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="stale-url",
                    resource_id="res-old",
                    token="old-token",
                    webhook_url="https://old-ngrok.example.com/webhooks/drive",
                    expires_at=NOW + timedelta(days=5),
                    start_page_token="SPT-OLD",
                    created_at=NOW - timedelta(days=1),
                ),
            )
            watch_channels.mark_active(session, "stale-url")

        # WHEN — the settings now point at a new URL
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — Drive is asked to stop the old channel and a new one is created
        drive.stop_channel.assert_called_once_with("stale-url", "res-old")
        drive.watch_changes.assert_called_once()
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"
        assert active.webhook_url == settings.webhook.url

    def test_survives_a_drive_stop_failure_and_still_renews(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN
        from sn2md_worker.drive.client import DriveClientError

        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="stale-url",
                    resource_id="res-old",
                    token="old-token",
                    webhook_url="https://old-ngrok.example.com/webhooks/drive",
                    expires_at=NOW + timedelta(days=5),
                    start_page_token="SPT-OLD",
                    created_at=NOW - timedelta(days=1),
                ),
            )
            watch_channels.mark_active(session, "stale-url")
        drive.stop_channel.side_effect = DriveClientError("channels.stop failed")

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — the failure was swallowed, the new channel was still created
        drive.watch_changes.assert_called_once()
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"


class TestWhenWebhookUrlIsNotConfigured:
    def test_skips_gracefully(self, engine: Engine, drive: MagicMock, tmp_path: Path) -> None:
        # GIVEN — settings without a webhook URL
        settings = Settings(
            drive=DriveConfig(source_folder_id="SRC"),
            webhook=WebhookConfig(url=""),
        )

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — no Drive call, no channel row
        drive.watch_changes.assert_not_called()
        with Session(engine) as session:
            assert watch_channels.get_active(session) is None


class TestEnsureActiveChannel:
    def test_skips_when_drive_client_is_none(self, engine: Engine, settings: Settings) -> None:
        # WHEN
        ensure_active_channel(None, settings)

        # THEN — no channel written
        with Session(engine) as session:
            assert watch_channels.get_active(session) is None

    def test_verifies_headroom_matches_constant(self) -> None:
        assert timedelta(hours=24) == RENEWAL_HEADROOM
