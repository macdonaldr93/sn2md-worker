"""BDD-style tests for /healthz, /readyz, and /status."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.app import create_app
from sn2md_worker.config import (
    ObservabilityConfig,
    Settings,
    WebhookConfig,
    set_settings,
)
from sn2md_worker.db import set_engine
from sn2md_worker.state import conversions, cursor, watch_channels
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.state.watch_channels import NewWatchChannel

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'obs.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


def _settings(*, webhook_url: str = "", status_enabled: bool = True) -> Settings:
    return Settings(
        webhook=WebhookConfig(url=webhook_url),
        observability=ObservabilityConfig(log_level="INFO", status_endpoint_enabled=status_enabled),
    )


class TestHealthz:
    def test_always_returns_200(self, engine: Engine) -> None:
        set_settings(_settings())
        client = TestClient(create_app())
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestCorrelationIdMiddleware:
    def test_generates_and_returns_a_request_id_when_none_is_supplied(self, engine: Engine) -> None:
        # GIVEN
        set_settings(_settings())
        client = TestClient(create_app())

        # WHEN
        response = client.get("/healthz")

        # THEN — a fresh id is generated and echoed back
        assert response.status_code == 200
        assert response.headers.get("x-request-id")
        assert len(response.headers["x-request-id"]) == 16

    def test_echoes_the_supplied_request_id_unchanged(self, engine: Engine) -> None:
        # GIVEN
        set_settings(_settings())
        client = TestClient(create_app())

        # WHEN
        response = client.get("/healthz", headers={"X-Request-Id": "trace-me-abc123"})

        # THEN
        assert response.headers["x-request-id"] == "trace-me-abc123"


class TestReadyzInDevMode:
    def test_returns_200_when_webhook_url_is_empty(self, engine: Engine) -> None:
        # GIVEN — no webhook configured
        set_settings(_settings(webhook_url=""))
        client = TestClient(create_app())

        # WHEN
        response = client.get("/readyz")

        # THEN
        assert response.status_code == 200


class TestReadyzInProdMode:
    def test_returns_503_when_no_active_channel(self, engine: Engine) -> None:
        # GIVEN — webhook URL configured but no channel row
        set_settings(_settings(webhook_url="https://example.com/webhook"))
        client = TestClient(create_app())

        # WHEN
        response = client.get("/readyz")

        # THEN
        assert response.status_code == 503

    def test_returns_200_when_active_channel_is_valid(self, engine: Engine) -> None:
        # GIVEN — an active channel with plenty of TTL
        set_settings(_settings(webhook_url="https://example.com/webhook"))
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="chan-1",
                    resource_id="res-1",
                    token="tok",
                    expires_at=NOW + timedelta(days=5),
                    start_page_token="1",
                    created_at=NOW,
                ),
            )
            watch_channels.mark_active(session, "chan-1")
        client = TestClient(create_app())

        # WHEN
        response = client.get("/readyz")

        # THEN
        assert response.status_code == 200

    def test_returns_503_when_active_channel_has_expired(self, engine: Engine) -> None:
        # GIVEN — an active channel whose expiration is in the past
        set_settings(_settings(webhook_url="https://example.com/webhook"))
        with Session(engine) as session, session.begin():
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="chan-1",
                    resource_id="res-1",
                    token="tok",
                    expires_at=datetime(2020, 1, 1, tzinfo=UTC),
                    start_page_token="1",
                    created_at=NOW,
                ),
            )
            watch_channels.mark_active(session, "chan-1")
        client = TestClient(create_app())

        # WHEN
        response = client.get("/readyz")

        # THEN
        assert response.status_code == 503


class TestStatusEndpoint:
    def test_returns_recent_success_failures_channel_and_cursor(self, engine: Engine) -> None:
        # GIVEN — one successful conversion, one failure, an active channel, and a cursor
        set_settings(_settings())
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key="Notebooks/success.note",
                    current_file_id="ok-1",
                    parent_folder_id="p1",
                    source_name="success.note",
                    source_path="Notebooks/success.note",
                    source_md5="md5-ok",
                    output_rel_path="Notebooks/success",
                    last_converted_at=NOW,
                    status=ConversionStatus.SUCCESS,
                ),
            )
            conversions.record_failure(
                session,
                logical_key="Notebooks/fail.note",
                current_file_id="err-1",
                source_name="fail.note",
                source_path="Notebooks/fail.note",
                error="gemini timeout",
                when=NOW - timedelta(minutes=5),
            )
            watch_channels.create(
                session,
                NewWatchChannel(
                    channel_id="chan-1",
                    resource_id="res-1",
                    token="tok",
                    expires_at=NOW + timedelta(days=5),
                    start_page_token="42",
                    created_at=NOW,
                ),
            )
            watch_channels.mark_active(session, "chan-1")
            cursor.set_cursor(session, "42", NOW)

        client = TestClient(create_app())

        # WHEN
        response = client.get("/status")

        # THEN
        assert response.status_code == 200
        body = response.json()
        assert [c["logical_key"] for c in body["recent_conversions"]] == ["Notebooks/success.note"]
        assert [c["last_error"] for c in body["recent_failures"]] == ["gemini timeout"]
        assert body["watch_channel"]["channel_id"] == "chan-1"
        assert body["watch_channel"]["is_active"] is True
        assert body["change_cursor"]["page_token"] == "42"

    def test_reports_zero_queue_depth_when_workflow_status_table_is_absent(
        self, engine: Engine
    ) -> None:
        # GIVEN — no DBOS init, so `workflow_status` doesn't exist in the test DB
        set_settings(_settings())
        client = TestClient(create_app())

        # WHEN
        body = client.get("/status").json()

        # THEN — the query gracefully falls back to defaults instead of 500ing
        assert body["queue_depth"] == {"convert_queue": 0, "poll_queue": 0}
        assert body["backfill"] == {
            "status": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
        }

    def test_returns_404_when_disabled(self, engine: Engine) -> None:
        # GIVEN
        set_settings(_settings(status_enabled=False))
        client = TestClient(create_app())

        # WHEN
        response = client.get("/status")

        # THEN
        assert response.status_code == 404
