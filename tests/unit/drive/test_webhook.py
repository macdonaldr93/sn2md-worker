from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from sn2md_worker.app import create_app
from sn2md_worker.workflows import POLL_QUEUE_NAME
from sn2md_worker.workflows.poll_changes import poll_changes


class TestWhenGoogleSendsSyncHandshake:
    def test_returns_200_without_enqueuing_work(self) -> None:
        # GIVEN
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": "test-channel",
                    "X-Goog-Channel-Token": "test-token",
                    "X-Goog-Resource-Id": "res-1",
                    "X-Goog-Resource-State": "sync",
                    "X-Goog-Message-Number": "1",
                },
            )

        # THEN
        assert response.status_code == 200
        enqueue.assert_not_called()


class TestWhenGoogleSendsChangeNotification:
    def test_returns_200_and_enqueues_poll_changes(self) -> None:
        # GIVEN
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post(
                "/webhooks/drive",
                headers={
                    "X-Goog-Channel-Id": "test-channel",
                    "X-Goog-Channel-Token": "test-token",
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


class TestWhenWebhookIsCalledWithoutGoogHeaders:
    def test_still_returns_200_and_enqueues(self) -> None:
        # GIVEN
        client = TestClient(create_app())

        # WHEN
        with patch("sn2md_worker.drive.webhook.DBOS.enqueue_workflow") as enqueue:
            response = client.post("/webhooks/drive")

        # THEN
        assert response.status_code == 200
        # Missing X-Goog-Resource-State means "not sync" → treated as a
        # notification and enqueued. Defensible default; would rather ack
        # a stray probe than 500.
        enqueue.assert_called_once()
