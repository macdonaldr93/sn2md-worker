from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from sn2md_worker.app import create_app
from sn2md_worker.config import (
    ObservabilityConfig,
    Settings,
    WebhookConfig,
    set_settings,
)
from sn2md_worker.db import set_engine
from sn2md_worker.startup_status import StartupStatus, set_startup_status
from sn2md_worker.state import conversions, cursor, watch_channels
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.state.watch_channels import NewWatchChannel, WatchChannelView

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'obs.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze `now_utc()` in observability so `/readyz` decisions are
    deterministic regardless of the calendar date CI happens to run on."""
    monkeypatch.setattr("sn2md_worker.observability.now_utc", lambda: NOW)


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


class TestRequestIdMiddleware:
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
                    webhook_url="https://example.com/webhooks/drive",
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
                    webhook_url="https://example.com/webhooks/drive",
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

    def test_returns_503_gracefully_even_if_expires_at_is_tz_naive(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN — patched `get_active` returns a WatchChannelView whose
        # expires_at is tz-naive (simulating a schema drift where the
        # UTCDateTime decorator got stripped). Without normalization this
        # would raise TypeError and 500 out.
        set_settings(_settings(webhook_url="https://example.com/webhook"))
        naive_expired = datetime(2020, 1, 1)  # noqa: DTZ001

        def fake_get_active(_session: Session) -> WatchChannelView:
            return WatchChannelView(
                channel_id="chan-1",
                resource_id="res-1",
                token="tok",
                webhook_url="https://example.com/webhooks/drive",
                expires_at=naive_expired,
                start_page_token="1",
                created_at=datetime(2020, 1, 1),  # noqa: DTZ001
                is_active=True,
            )

        monkeypatch.setattr("sn2md_worker.observability.watch_channels.get_active", fake_get_active)
        client = TestClient(create_app())

        # WHEN
        response = client.get("/readyz")

        # THEN — still 503, not 500
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
                    webhook_url="https://example.com/webhooks/drive",
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
        assert body["recent_pending"] == []
        assert body["watch_channel"]["channel_id"] == "chan-1"
        assert body["watch_channel"]["is_active"] is True
        assert body["change_cursor"]["page_token"] == "42"

    def test_surfaces_pending_conversions_so_in_flight_and_stuck_notes_are_visible(
        self, engine: Engine
    ) -> None:
        # GIVEN — a PENDING record (mid-conversion or crashed mid-note).
        set_settings(_settings())
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key="Notebooks/pending.note",
                    current_file_id="pen-1",
                    parent_folder_id="p1",
                    source_name="pending.note",
                    source_path="Notebooks/pending.note",
                    source_md5="md5-pending",
                    output_rel_path="Notebooks/pending",
                    last_converted_at=NOW,
                    status=ConversionStatus.PENDING,
                ),
            )

        client = TestClient(create_app())

        # WHEN
        body = client.get("/status").json()

        # THEN — surfaced in its own bucket, not in success/error buckets.
        assert [c["logical_key"] for c in body["recent_pending"]] == ["Notebooks/pending.note"]
        assert body["recent_conversions"] == []
        assert body["recent_failures"] == []

    def test_reports_correct_queue_depth_when_workflow_status_rows_exist(
        self, engine: Engine
    ) -> None:
        # GIVEN — hand-crafted `workflow_status` table with a mix of
        # terminal (SUCCESS, ERROR) and non-terminal (PENDING, ENQUEUED)
        # rows across both queues, so we can prove the `NOT IN` binding
        # actually filters correctly.
        set_settings(_settings())
        with Session(engine) as session, session.begin():
            session.execute(
                text(
                    "CREATE TABLE workflow_status ("
                    "  workflow_id TEXT PRIMARY KEY,"
                    "  queue_name TEXT,"
                    "  status TEXT"
                    ")"
                )
            )
            for wf_id, queue, status_ in [
                ("w1", "convert_queue", "PENDING"),
                ("w2", "convert_queue", "ENQUEUED"),
                ("w3", "convert_queue", "SUCCESS"),  # terminal → excluded
                ("w4", "poll_queue", "PENDING"),
                ("w5", "poll_queue", "ERROR"),  # terminal → excluded
                ("w6", None, "PENDING"),  # queue_name NULL → excluded
            ]:
                session.execute(
                    text(
                        "INSERT INTO workflow_status (workflow_id, queue_name, status) "
                        "VALUES (:id, :q, :s)"
                    ),
                    {"id": wf_id, "q": queue, "s": status_},
                )

        client = TestClient(create_app())

        # WHEN
        body = client.get("/status").json()

        # THEN — 2 non-terminal in convert_queue, 1 in poll_queue
        assert body["queue_depth"] == {"convert_queue": 2, "poll_queue": 1}

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

    def test_startup_defaults_to_all_deferred_when_none_recorded(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN — no startup status has been set (test bypasses __main__.py).
        # Force the singleton back to None in case another test recorded one.
        monkeypatch.setattr("sn2md_worker.startup_status._Holder.status", None)
        set_settings(_settings())
        client = TestClient(create_app())

        # WHEN
        body = client.get("/status").json()

        # THEN — every field is "deferred", no error
        assert body["startup"] == {
            "drive_client": "deferred",
            "seed_cursor": "deferred",
            "ensure_channel": "deferred",
            "backfill_enqueue": "deferred",
            "last_error": None,
        }

    def test_startup_reports_recorded_step_outcomes(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GIVEN — a startup where DriveClient came up but seeding cursor failed
        monkeypatch.setattr("sn2md_worker.startup_status._Holder.status", None)
        set_startup_status(
            StartupStatus(
                drive_client="ok",
                seed_cursor="failed",
                ensure_channel="ok",
                backfill_enqueue="ok",
                last_error="seed_cursor: Drive transport failure",
            )
        )
        set_settings(_settings())
        client = TestClient(create_app())

        # WHEN
        body = client.get("/status").json()

        # THEN — outcomes surface faithfully
        assert body["startup"] == {
            "drive_client": "ok",
            "seed_cursor": "failed",
            "ensure_channel": "ok",
            "backfill_enqueue": "ok",
            "last_error": "seed_cursor: Drive transport failure",
        }
