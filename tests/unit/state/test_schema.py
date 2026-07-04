from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from sn2md_worker.state.schema import init_schema


def test_init_schema_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite"
    url = f"sqlite:///{db_path}"

    init_schema(url)

    engine = create_engine(url, future=True)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert {
        "conversion_records",
        "drive_watch_channels",
        "drive_change_cursor",
        "debounce_state",
    } <= tables


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite"
    url = f"sqlite:///{db_path}"

    init_schema(url)
    init_schema(url)  # no exception, no duplicate tables
