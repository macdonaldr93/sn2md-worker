from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from sn2md_worker.db import create_engine_for
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
        finally:
            engine.dispose()
        assert mode.lower() == "wal"
        assert busy == 30000
        assert synchronous == 1  # NORMAL
        assert fk == 1
