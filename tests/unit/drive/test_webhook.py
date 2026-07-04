from __future__ import annotations

from fastapi.testclient import TestClient

from sn2md_worker.app import create_app


def test_sync_handshake_returns_200() -> None:
    client = TestClient(create_app())

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

    assert response.status_code == 200


def test_change_notification_returns_200() -> None:
    client = TestClient(create_app())

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

    assert response.status_code == 200


def test_webhook_without_goog_headers_still_returns_200() -> None:
    client = TestClient(create_app())

    response = client.post("/webhooks/drive")

    assert response.status_code == 200
