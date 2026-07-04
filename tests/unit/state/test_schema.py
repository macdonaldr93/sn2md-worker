from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

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
        engine = create_engine(url, future=True)
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
