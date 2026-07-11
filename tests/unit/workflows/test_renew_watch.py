from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import DriveConfig, Settings, WebhookConfig
from sn2md_worker.db import set_engine
from sn2md_worker.drive.client import DriveClient, DriveClientError
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
        drive=DriveConfig(source_folder_id="SRC"),
        webhook=WebhookConfig(url="https://sn2md.example.com/webhooks/drive"),
    )


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze `now_utc()` in `renew_watch` so `ensure_active_channel`'s
    freshness check compares against a fixed instant instead of the
    CI host's wall clock (which drifts past the fixture's `NOW + Nd`)."""
    monkeypatch.setattr("sn2md_worker.workflows.renew_watch.now_utc", lambda: NOW)


@pytest.fixture(autouse=True)
def stable_channel_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the channel_id UUID that `_create_and_activate` generates so
    the pre-persisted pending row and the follow-up `confirm` land on the
    same predictable value."""

    class _FakeUUID:
        hex = "new-channel"

    monkeypatch.setattr(
        "sn2md_worker.workflows.renew_watch.uuid.uuid4",
        _FakeUUID,
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
        # No ttl_seconds — we let Google pick its max (7 days) rather than
        # computing expiry from the local wall clock.
        assert "ttl_seconds" not in kwargs

        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"
        # We now store OUR generated token, not whatever Drive echoes back,
        # so the DB row matches the token we sent (and the token Drive is
        # holding on its side).
        assert active.token == kwargs["token"]

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


class TestWhenDriveWatchChangesFailsMidRenewal:
    def test_rolls_back_the_pending_row_and_reraises(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — Drive rejects the watch.changes request
        drive.watch_changes.side_effect = RuntimeError("drive is grumpy")

        # WHEN / THEN — the workflow re-raises...
        with pytest.raises(RuntimeError, match="grumpy"):
            renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # AND — the pending row does not linger (rolled back)
        with Session(engine) as session:
            leftovers = watch_channels.list_all(session)
        assert leftovers == []


class TestWhenRenewalSucceedsRowGoesThroughPendingThenActive:
    def test_pre_persist_row_is_updated_with_real_drive_values(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — capture the row state right when Drive is called, so we
        # can prove the pre-persist happened before the HTTP call.
        captured_channel_id: dict[str, str] = {}

        def capture_pending(**kwargs: object) -> ChannelInfo:
            captured_channel_id["id"] = str(kwargs["channel_id"])
            with Session(engine) as sess:
                rows = watch_channels.list_all(sess)
            captured_channel_id["pre_persist_count"] = str(len(rows))
            captured_channel_id["pre_persist_resource_id"] = rows[0].resource_id if rows else ""
            captured_channel_id["pre_persist_is_active"] = "1" if rows[0].is_active else "0"
            return ChannelInfo(
                id="new-channel",
                resource_id="res-1",
                expiration=NOW + timedelta(days=7),
                token="server-token",
            )

        drive.watch_changes.side_effect = capture_pending

        # WHEN
        renew_watch_channel_impl(trigger_source="test", drive=drive, settings=settings, now=NOW)

        # THEN — mid-Drive-call there was one row (pending), inactive,
        # with an empty resource_id (the placeholder).
        assert captured_channel_id["pre_persist_count"] == "1"
        assert captured_channel_id["pre_persist_resource_id"] == ""
        assert captured_channel_id["pre_persist_is_active"] == "0"

        # AND — after Drive succeeds, that same row is now confirmed and active
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"
        assert active.resource_id == "res-1"
        assert active.is_active is True


class TestEnsureActiveChannel:
    def test_skips_when_drive_client_is_none(self, engine: Engine, settings: Settings) -> None:
        # WHEN
        ensure_active_channel(None, settings)

        # THEN — no channel written
        with Session(engine) as session:
            assert watch_channels.get_active(session) is None

    def test_verifies_headroom_matches_constant(self) -> None:
        assert timedelta(hours=48) == RENEWAL_HEADROOM


class TestEnsureActiveChannelWhenPreviousChannelHasExpired:
    def test_enqueues_a_recovery_poll_and_creates_a_new_channel(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — an active channel whose expires_at is already in the past.
        # We build one via `watch_channels.create` + `mark_active` so we
        # don't have to reason about the two-phase renew flow.
        expired_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="stale",
                    resource_id="res-stale",
                    token="tok-stale",
                    webhook_url=settings.webhook.url,
                    expires_at=expired_at,
                    start_page_token="SPT-STALE",
                    created_at=expired_at - timedelta(days=8),
                ),
            )
            watch_channels.mark_active(session, "stale")

        # AND — the drive stop-old-channel call is stubbed to succeed
        drive.stop_channel.return_value = None

        # WHEN
        with patch("sn2md_worker.workflows.renew_watch.DBOS.enqueue_workflow") as enqueue:
            ensure_active_channel(drive, settings)

        # THEN — a `poll_changes("recovery")` was enqueued (via the
        # POLL_QUEUE_NAME queue) to catch up on missed notifications
        assert enqueue.call_count == 1
        args, _ = enqueue.call_args
        assert args[0] == "poll_queue"
        assert args[2] == "recovery"

        # AND — the normal renewal flow still ran (new channel is active)
        with Session(engine) as session:
            active = watch_channels.get_active(session)
        assert active is not None
        assert active.channel_id == "new-channel"

    def test_does_not_enqueue_recovery_when_previous_channel_is_still_fresh(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — an active channel that is still valid (expires in 3d)
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="fresh",
                    resource_id="res-fresh",
                    token="tok-fresh",
                    webhook_url=settings.webhook.url,
                    expires_at=NOW + timedelta(days=3),
                    start_page_token="SPT-FRESH",
                    created_at=NOW - timedelta(days=4),
                ),
            )
            watch_channels.mark_active(session, "fresh")

        # WHEN
        with patch("sn2md_worker.workflows.renew_watch.DBOS.enqueue_workflow") as enqueue:
            ensure_active_channel(drive, settings)

        # THEN — no recovery poll enqueued (channel is fresh)
        enqueue.assert_not_called()
