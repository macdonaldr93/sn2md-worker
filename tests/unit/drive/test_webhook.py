from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from dbos._error import DBOSQueueDeduplicatedError  # noqa: PLC2701
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.app import create_app
from sn2md_worker.db import set_engine
from sn2md_worker.state import watch_channels
from sn2md_worker.state.models import Base
from sn2md_worker.state.watch_channels import NewWatchChannel
from sn2md_worker.workflows import POLL_QUEUE_NAME
from sn2md_worker.workflows.poll_changes import poll_changes

CHANNEL_ID = "chan-1"
CHANNEL_TOKEN = "tok-1"
NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'webhook.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze `now_utc()` in the webhook module so channel-expiry auth
    is stable across calendar dates in CI."""
    monkeypatch.setattr("sn2md_worker.drive.webhook.now_utc", lambda: NOW)


@pytest.fixture
def registered_channel(engine: Engine) -> None:
    now = NOW
    with Session(engine) as session, session.begin():
        watch_channels.create(
            session,
            NewWatchChannel(
                channel_id=CHANNEL_ID,
                resource_id="res-1",
                token=CHANNEL_TOKEN,
                webhook_url="https://example.com/webhooks/drive",
                expires_at=now + timedelta(days=6),
                start_page_token="1",
                created_at=now,
            ),
        )
        watch_channels.mark_active(session, CHANNEL_ID)


class TestWhenGoogleSendsSyncHandshake:
    def test_returns_200_without_enqueuing_work(self, engine: Engine) -> None:
        # GIVEN
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": CHANNEL_TOKEN,
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "sync",
                    "X-Goog-Message-Number": "1",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_not_called()


class TestWhenGoogleSendsAuthenticatedChangeNotification:
    def test_returns_200_and_enqueues_poll_changes(
        self, engine: Engine, registered_channel: None
    ) -> None:
        # GIVEN — a channel record exists matching id+token
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": CHANNEL_TOKEN,
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                    "X-Goog-Message-Number": "42",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == POLL_QUEUE_NAME
        assert args[1] is poll_changes
        assert args[2] == "webhook"


class TestWhenChannelIdIsUnknown:
    def test_still_returns_200_but_does_not_enqueue(self, engine: Engine) -> None:
        # GIVEN — no matching channel row
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": "unknown-channel",
                    "X-Goog-Channel-Token": "some-token",
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                    "X-Goog-Message-Number": "42",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_not_called()


class TestWhenTokenDoesNotMatch:
    def test_still_returns_200_but_does_not_enqueue(
        self, engine: Engine, registered_channel: None
    ) -> None:
        # GIVEN — the channel_id is known but token differs
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": "WRONG",
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                    "X-Goog-Message-Number": "42",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_not_called()


class TestWhenChannelIsExpired:
    def test_still_returns_200_but_does_not_enqueue(self, engine: Engine) -> None:
        # GIVEN — a channel whose expires_at is already in the past
        past = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id=CHANNEL_ID,
                    resource_id="res-1",
                    token=CHANNEL_TOKEN,
                    webhook_url="https://example.com/webhooks/drive",
                    expires_at=past - timedelta(days=1),
                    start_page_token="1",
                    created_at=past - timedelta(days=8),
                ),
            )
            watch_channels.mark_active(session, CHANNEL_ID)
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": CHANNEL_TOKEN,
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                    "X-Goog-Message-Number": "42",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_not_called()


class TestWhenGoogleRetriesTheSameMessage:
    def test_dbos_dedup_error_is_absorbed_and_response_is_still_200(
        self,
        engine: Engine,
        registered_channel: None,  # noqa: ARG002
    ) -> None:
        # GIVEN
        client = TestClient(create_app())

        def raise_dedup(*args: object, **kwargs: object) -> None:  # noqa: ARG001
            raise DBOSQueueDeduplicatedError(
                workflow_id="wf-1",
                queue_name=POLL_QUEUE_NAME,
                deduplication_id=f"{CHANNEL_ID}:42",
            )

        # WHEN
        with patch(
            "sn2md_worker.drive.webhook.DBOS.enqueue_workflow",
            side_effect=raise_dedup,
        ) as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": CHANNEL_TOKEN,
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                    "X-Goog-Message-Number": "42",
                },
            )

        # THEN — enqueue attempted once and absorbed as ack
        assert response.status_code == 200
        enqueue.assert_called_once()


class TestWhenAuthenticatedButMessageNumberIsMissing:
    def test_still_enqueues_without_dedup(
        self,
        engine: Engine,
        registered_channel: None,  # noqa: ARG002
    ) -> None:
        # GIVEN
        client = TestClient(create_app())

        # WHEN — no X-Goog-Message-Number header
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": CHANNEL_ID,
                    "X-Goog-Channel-Token": CHANNEL_TOKEN,
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "change",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_called_once()


class TestWhenGoogHeadersAreMissing:
    def test_returns_200_without_enqueue(self, engine: Engine) -> None:
        # GIVEN — no headers at all (stray probe)
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post("/webhooks/drive")

        # THEN — no channel id → cannot authenticate → no enqueue
        assert response.status_code == 200
        enqueue.assert_not_called()
