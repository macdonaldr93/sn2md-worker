from __future__ import annotations

from pathlib import Path

import pytest

from sn2md_worker.__main__ import (
    _dbos_config,
    _prepare_sqlite_dir,
    _run_boot_step,
    _try_init_drive_client,
)
from sn2md_worker.config import DatabaseConfig, GoogleConfig, Settings
from sn2md_worker.drive.client import DriveClientError
from sn2md_worker.logging import get_logger


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


class TestRunBootStepDefersWhenPrerequisiteMissing:
    def test_skip_true_returns_deferred_without_calling_action(self) -> None:
        # GIVEN
        calls = {"n": 0}

        def action() -> None:
            calls["n"] += 1

        # WHEN
        result = _run_boot_step("seed_cursor", action, get_logger("test"), skip=True)

        # THEN — action untouched; status deferred
        assert calls["n"] == 0
        assert result.status == "deferred"
        assert result.error is None


class TestRunBootStepReportsFailure:
    def test_drive_client_error_returns_failed_with_prefixed_message(self) -> None:
        # GIVEN
        def action() -> None:
            raise DriveClientError("Drive transport failure (TimeoutError)")

        # WHEN
        result = _run_boot_step("ensure_channel", action, get_logger("test"), skip=False)

        # THEN — status failed; error prefixed with the step name for grep
        assert result.status == "failed"
        assert result.error == "ensure_channel: Drive transport failure (TimeoutError)"

    def test_non_drive_client_exception_still_propagates(self) -> None:
        # GIVEN — a KeyError isn't a DriveClientError; boot bugs should
        # surface loudly instead of degrading silently.
        def action() -> None:
            raise KeyError("some_key")

        # WHEN / THEN
        with pytest.raises(KeyError):
            _run_boot_step("seed_cursor", action, get_logger("test"), skip=False)


class TestRunBootStepReportsOk:
    def test_happy_path_returns_ok(self) -> None:
        # GIVEN
        def action() -> None:
            return None

        # WHEN
        result = _run_boot_step("backfill_enqueue", action, get_logger("test"), skip=False)

        # THEN
        assert result.status == "ok"
        assert result.error is None


class TestTryInitDriveClientReportsMissingKeyAsDeferred:
    def test_missing_credentials_file_is_deferred_not_failed(self, tmp_path: Path) -> None:
        # GIVEN — a settings pointing at a path that doesn't exist
        settings = Settings(google=GoogleConfig(application_credentials=tmp_path / "missing.json"))

        # WHEN
        result = _try_init_drive_client(settings)

        # THEN — dev-mode path: deferred, no error
        assert result.status == "deferred"
        assert result.error is None


class TestTryInitDriveClientReportsMalformedKeyAsFailed:
    def test_malformed_json_is_failed_with_error_message(self, tmp_path: Path) -> None:
        # GIVEN — a file that exists but is not a valid service-account JSON
        creds = tmp_path / "sa.json"
        creds.write_text("{}")
        settings = Settings(google=GoogleConfig(application_credentials=creds))

        # WHEN
        result = _try_init_drive_client(settings)

        # THEN — surfaces as a failed boot step; container will still start,
        # `/status.startup.last_error` reports why.
        assert result.status == "failed"
        assert result.error is not None
        assert result.error.startswith("drive_client: ")
