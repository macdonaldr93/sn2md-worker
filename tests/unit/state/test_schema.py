from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from sn2md_worker.db import create_engine_for
from sn2md_worker.state import cursor
from sn2md_worker.state.schema import init_schema

EXPECTED_TABLES = {
    "conversion_records",
    "drive_watch_channels",
    "drive_change_cursor",
    "debounce_state",
}


class TestInitSchema:
    def test_creates_all_application_tables(self, tmp_path: Path) -> None:
        # GIVEN
        db_path = tmp_path / "test.sqlite"
        url = f"sqlite:///{db_path}"

        # WHEN
        init_schema(url)

        # THEN
        engine = create_engine_for(url)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert tables >= EXPECTED_TABLES

    def test_is_idempotent_when_run_twice(self, tmp_path: Path) -> None:
        # GIVEN
        db_path = tmp_path / "test.sqlite"
        url = f"sqlite:///{db_path}"

        # WHEN / THEN — second call must not raise or duplicate tables
        init_schema(url)
        init_schema(url)


class TestWhenSqliteConnectionOpens:
    def test_applies_wal_journal_mode(self, tmp_path: Path) -> None:
        # GIVEN
        db_path = tmp_path / "pragma.sqlite"
        url = f"sqlite:///{db_path}"
        init_schema(url)

        # WHEN / THEN
        engine = create_engine_for(url)
        try:
            with engine.connect() as conn:
                mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
                busy = conn.execute(text("PRAGMA busy_timeout")).scalar_one()
                synchronous = conn.execute(text("PRAGMA synchronous")).scalar_one()
                fk = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
                raw_conn = conn.connection.dbapi_connection
                isolation_level = raw_conn.isolation_level if raw_conn is not None else None
        finally:
            engine.dispose()
        assert mode.lower() == "wal"
        assert busy == 30000
        assert synchronous == 1  # NORMAL
        assert fk == 1
        assert isolation_level == "IMMEDIATE"

    def test_concurrent_read_then_write_transactions_wait_instead_of_failing_immediately(
        self, tmp_path: Path
    ) -> None:
        # GIVEN — a file-backed SQLite database where two sessions both read
        # and then write. Under sqlite3's default DEFERRED transactions, the
        # second session's read-to-write lock upgrade fails immediately with
        # SQLITE_BUSY (ignoring busy_timeout). With BEGIN IMMEDIATE, the
        # second transaction waits for the first writer to commit.
        db_path = tmp_path / "concurrent.sqlite"
        url = f"sqlite:///{db_path}"
        init_schema(url)
        engine = create_engine_for(url)
        now = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
        second_started = threading.Event()
        errors: list[BaseException] = []

        def writer_two() -> None:
            try:
                second_started.set()
                with Session(engine) as sess2, sess2.begin():
                    _ = cursor.get(sess2)
                    cursor.set_cursor(sess2, "token-2", now)
            except BaseException as exc:
                errors.append(exc)

        try:
            # WHEN — session 1 holds a write transaction open briefly while
            # session 2 starts a read-then-write transaction concurrently
            with Session(engine) as sess1, sess1.begin():
                _ = cursor.get(sess1)
                cursor.set_cursor(sess1, "token-1", now)
                sess1.flush()
                t = threading.Thread(target=writer_two)
                t.start()
                second_started.wait(timeout=2.0)
                time.sleep(0.05)
            t.join(timeout=5.0)

            # THEN — neither transaction raised OperationalError("database is locked")
            assert errors == []
            with Session(engine) as verify_sess:
                saved = cursor.get(verify_sess)
            assert saved is not None
            assert saved.page_token == "token-2"
        finally:
            engine.dispose()
