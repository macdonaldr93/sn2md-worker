from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from dbos import DBOS, DBOSConfig
from sqlalchemy.engine import make_url

from sn2md_worker.app import create_app
from sn2md_worker.config import Settings, load_settings, set_settings
from sn2md_worker.db import (
    create_datasource,
    create_engine_for,
    set_datasource,
    set_engine,
)
from sn2md_worker.drive.client import (
    DriveClient,
    DriveClientError,
    get_drive_client,
    set_drive_client,
)
from sn2md_worker.logging import configure_logging, get_logger
from sn2md_worker.startup_status import BootStepResult, StartupStatus, set_startup_status
from sn2md_worker.state import init_schema


def main() -> int:
    settings = load_settings()
    configure_logging(settings.observability.log_level)
    log = get_logger("sn2md_worker")

    _prepare_sqlite_dir(settings.database.url)

    app = create_app()

    dbos_config = _dbos_config(settings)
    # `render_as_string(hide_password=True)` replaces any embedded userinfo
    # password with `***`. Today the URL is `sqlite:////data/…` with no
    # secrets in it, but that changes the day someone points at Postgres.
    log.info(
        "dbos_init",
        database_url=make_url(settings.database.url).render_as_string(hide_password=True),
    )
    DBOS(config=dbos_config, fastapi=app)

    init_schema(settings.database.url)
    log.info("app_schema_ready")

    set_settings(settings)
    set_engine(create_engine_for(settings.database.url))
    set_datasource(create_datasource(settings.database.url))
    log.info("datasource_ready")

    drive_result = _try_init_drive_client(settings)

    # Importing the workflows package registers @DBOS.workflow() decorators.
    # Deferred so that settings/engine/drive-client singletons are ready
    # before workflow modules resolve them at import time.
    from sn2md_worker import workflows  # noqa: PLC0415

    drive_client = _current_drive_client()
    skip_drive_steps = drive_client is None

    # DB-only, safe before DBOS is launched.
    seed_result = _run_boot_step(
        "seed_cursor",
        lambda: workflows.seed_cursor_if_ready(drive_client),
        log,
        skip=skip_drive_steps,
    )

    DBOS.launch()
    log.info("dbos_launched")

    # register_queue needs a launched DBOS; a webhook that arrives in the
    # micro-window before this point will fail to enqueue, and Google will
    # retry with exponential backoff — acceptable.
    workflows.register_queues()
    log.info("queues_registered")

    workflows.register_schedules()
    log.info("schedules_registered")

    channel_result = _run_boot_step(
        "ensure_channel",
        lambda: workflows.ensure_active_channel_if_ready(drive_client, settings),
        log,
        skip=skip_drive_steps,
    )

    backfill_result = _run_boot_step(
        "backfill_enqueue",
        workflows.enqueue_startup_backfill,
        log,
        skip=skip_drive_steps,
    )
    if backfill_result.status == "ok":
        log.info("backfill_enqueued")

    set_startup_status(
        StartupStatus(
            drive_client=drive_result.status,
            seed_cursor=seed_result.status,
            ensure_channel=channel_result.status,
            backfill_enqueue=backfill_result.status,
            last_error=next(
                (
                    r.error
                    for r in (drive_result, seed_result, channel_result, backfill_result)
                    if r.error
                ),
                None,
            ),
        )
    )

    # Single worker is load-bearing: SQLite + DBOS + our singletons all live
    # in-process. Multi-worker would re-run DBOS(...), init_schema, and
    # create_schedule per worker and race on the shared SQLite file. Do not
    # switch to `workers=N`.
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1, log_config=None)
    return 0


def _run_boot_step(
    name: str,
    action: Callable[[], Any],
    log: Any,
    *,
    skip: bool,
) -> BootStepResult:
    """Run one boot-time init step; log-and-continue on DriveClientError.

    `skip=True` means the prerequisite (usually DriveClient) wasn't ready
    and we intentionally didn't try — status becomes `deferred`, no
    error recorded. A real failure (`DriveClientError` raised by the
    action) returns `BootStepResult("failed", "<step>: <error>")`. Any
    other exception still propagates — those represent programming
    errors we want to see at boot, not silently swallow.
    """
    if skip:
        return BootStepResult(status="deferred", error=None)
    try:
        action()
    except DriveClientError as exc:
        log.warning("boot_step_failed", step=name, error=str(exc))
        return BootStepResult(status="failed", error=f"{name}: {exc}")
    return BootStepResult(status="ok", error=None)


def _dbos_config(settings: Settings) -> DBOSConfig:
    config = DBOSConfig(
        name="sn2md-worker",
        system_database_url=settings.database.url,
    )
    # DBOS's internal SystemDatabase / ApplicationDatabase engines don't
    # set `check_same_thread=False` by default (see .venv .../dbos/
    # _sys_db_sqlite.py: `_create_engine` just forwards engine_kwargs).
    # FastAPI's threadpool + DBOS's `dbos-executor-*` pool share a QueuePool,
    # so a sqlite3 Connection would eventually get handed to a thread it
    # wasn't opened in and raise `sqlite3.ProgrammingError`. Forwarding
    # `db_engine_kwargs` here fixes both engines — DBOS copies these
    # into `sys_db_engine_kwargs` during config translation.
    if settings.database.url.startswith("sqlite"):
        config["db_engine_kwargs"] = {
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
    return config


def _current_drive_client() -> DriveClient | None:
    try:
        return get_drive_client()
    except RuntimeError:
        return None


def _try_init_drive_client(settings: Settings) -> BootStepResult:
    """Best-effort DriveClient init; returns the boot-step outcome.

    `deferred` — credentials file simply isn't present (dev mode / not
    yet configured). `failed` — file is present but malformed or
    rejected; the container still boots so `/healthz` responds and
    `/status.startup.last_error` reports why.
    """
    log = get_logger("sn2md_worker")
    creds_path = settings.google.application_credentials
    if not creds_path.is_file():
        log.warning("drive_client_skipped_no_credentials", path=str(creds_path))
        return BootStepResult(status="deferred", error=None)
    try:
        client = DriveClient(creds_path)
    except DriveClientError as exc:
        log.error("drive_client_init_failed", error=str(exc))
        return BootStepResult(status="failed", error=f"drive_client: {exc}")
    set_drive_client(client)
    log.info("drive_client_ready", service_account=client.service_account_email)
    return BootStepResult(status="ok", error=None)


def _prepare_sqlite_dir(database_url: str) -> None:
    """Ensure the parent directory of a SQLite database file exists.

    Uses `make_url` so `sqlite+pysqlite://` and any other SQLAlchemy
    variant parse cleanly instead of string-slicing on `sqlite://`.
    `:memory:` and non-SQLite URLs are no-ops.
    """
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        return
    if not parsed.database or parsed.database == ":memory:":
        return
    Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
