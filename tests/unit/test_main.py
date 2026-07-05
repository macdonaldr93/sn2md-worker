from __future__ import annotations

from pathlib import Path

from sn2md_worker.__main__ import _dbos_config, _prepare_sqlite_dir
from sn2md_worker.config import DatabaseConfig, Settings


class TestPrepareSqliteDirCreatesTheParentDirectory:
    def test_absolute_sqlite_path_creates_missing_parent(self, tmp_path: Path) -> None:
        # GIVEN — a path whose parent does not yet exist
        target = tmp_path / "state" / "app.sqlite"
        assert not target.parent.exists()

        # WHEN
        _prepare_sqlite_dir(f"sqlite:///{target}")

        # THEN
        assert target.parent.exists()

    def test_pysqlite_variant_is_parsed_via_make_url(self, tmp_path: Path) -> None:
        # GIVEN — the `sqlite+pysqlite://` variant used by DBOS internally.
        # String-slicing on `sqlite://` would trip; make_url handles both.
        target = tmp_path / "pysqlite" / "app.sqlite"

        # WHEN
        _prepare_sqlite_dir(f"sqlite+pysqlite:///{target}")

        # THEN
        assert target.parent.exists()


class TestPrepareSqliteDirSkipsNoRelevantPaths:
    def test_memory_url_creates_nothing(self, tmp_path: Path) -> None:
        # GIVEN / WHEN — no side effects for :memory:
        _prepare_sqlite_dir("sqlite:///:memory:")

        # THEN — tmp_path has nothing in it (fixture cleanup covers rest)
        assert list(tmp_path.iterdir()) == []

    def test_non_sqlite_url_is_a_noop(self, tmp_path: Path) -> None:
        # GIVEN / WHEN
        _prepare_sqlite_dir("postgresql://user:pw@host/db")

        # THEN — no directory created; no exception raised
        assert list(tmp_path.iterdir()) == []


class TestDbosConfigForcesCheckSameThreadFalseOnSqlite:
    def test_sqlite_url_gets_connect_args_check_same_thread_false(self) -> None:
        # GIVEN — a SQLite URL (the container default)
        settings = Settings(database=DatabaseConfig(url="sqlite:////data/app.sqlite"))

        # WHEN
        config = _dbos_config(settings)

        # THEN — DBOS receives our SQLite-friendly connect_args, which its
        # config translation copies to both `db_engine_kwargs` and
        # `sys_db_engine_kwargs`. Without this, DBOS's internal engines
        # default to check_same_thread=True and blow up under threadpool use.
        assert config["db_engine_kwargs"] == {  # type: ignore[typeddict-item]
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }

    def test_non_sqlite_url_omits_engine_kwargs(self) -> None:
        # GIVEN
        settings = Settings(database=DatabaseConfig(url="postgresql://user:pw@host/db"))

        # WHEN
        config = _dbos_config(settings)

        # THEN — no override; DBOS uses its Postgres defaults
        assert "db_engine_kwargs" not in config
