from __future__ import annotations

import sys
from pathlib import Path

import uvicorn
from dbos import DBOS, DBOSConfig

from sn2md_worker.app import create_app
from sn2md_worker.config import Settings, load_settings
from sn2md_worker.db import create_datasource, set_datasource
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

    datasource = create_datasource(settings.database.url)
    set_datasource(datasource)
    log.info("datasource_ready")

    DBOS.launch()
    log.info("dbos_launched")

    uvicorn.run(app, host="0.0.0.0", port=8080, log_config=None)
    return 0


def _dbos_config(settings: Settings) -> DBOSConfig:
    return DBOSConfig(
        name="sn2md-worker",
        system_database_url=settings.database.url,
    )


def _prepare_sqlite_dir(database_url: str) -> None:
    if not database_url.startswith("sqlite:"):
        return
    raw = database_url.split("sqlite://", 1)[1]
    fs_path = Path(raw[1:]) if raw.startswith("//") else Path(raw.lstrip("/"))
    fs_path.parent.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
