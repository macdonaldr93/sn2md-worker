from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from dbos import DBOS, DBOSConfig

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
from sn2md_worker.state import init_schema


def main() -> int:
    settings = load_settings()
    configure_logging(settings.observability.log_level)
    log = get_logger("sn2md_worker")

    _prepare_sqlite_dir(settings.database.url)

    app = create_app()

    dbos_config = _dbos_config(settings)
    log.info("dbos_init", database_url=dbos_config["system_database_url"])
    DBOS(config=dbos_config, fastapi=app)

    init_schema(settings.database.url)
    log.info("app_schema_ready")

    set_settings(settings)
    set_engine(create_engine_for(settings.database.url))
    set_datasource(create_datasource(settings.database.url))
    log.info("datasource_ready")

    _try_init_drive_client(settings)

    # Importing the workflows package registers @DBOS.workflow() decorators.
    from sn2md_worker import workflows

    # DB-only, safe before DBOS is launched.
    workflows.seed_cursor_if_ready(_current_drive_client())

    DBOS.launch()
    log.info("dbos_launched")

    # register_queue needs a launched DBOS; a webhook that arrives in the
    # micro-window before this point will fail to enqueue, and Google will
    # retry with exponential backoff — acceptable.
    workflows.register_queues()
    log.info("queues_registered")

    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    return 0


def _dbos_config(settings: Settings) -> DBOSConfig:
    return DBOSConfig(
        name="sn2md-worker",
        system_database_url=settings.database.url,
    )


def _current_drive_client() -> DriveClient | None:
    try:
        return get_drive_client()
    except RuntimeError:
        return None


def _try_init_drive_client(settings: Settings) -> None:
    log = get_logger("sn2md_worker")
    creds_path = settings.google.application_credentials
    if not creds_path.is_file():
        log.warning("drive_client_skipped_no_credentials", path=str(creds_path))
        return
    try:
        client = DriveClient(creds_path)
    except DriveClientError as exc:
        log.error("drive_client_init_failed", error=str(exc))
        return
    set_drive_client(client)
    log.info("drive_client_ready", service_account=client.service_account_email)


def _prepare_sqlite_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite:"):
        return
    raw = database_url.split("sqlite://", 1)[1]
    fs_path = Path(raw[1:]) if raw.startswith("//") else Path(raw.lstrip("/"))
    fs_path.parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
