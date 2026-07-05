from __future__ import annotations

import sqlite3
from typing import Any

from dbos import SQLAlchemyDatasource
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

__all__ = [
    "create_datasource",
    "create_engine_for",
    "get_datasource",
    "get_engine",
    "set_datasource",
    "set_engine",
    "sql_session",
]


def create_datasource(database_url: str) -> SQLAlchemyDatasource:
    """Build a SQLAlchemyDatasource pointed at the shared application DB.

    For SQLite URLs, hands DBOS `connect_args` so its datasource engine
    gets the same driver-level timeout and thread policy as ours.
    """
    return SQLAlchemyDatasource.create(
        database_url,
        engine_kwargs=_engine_kwargs(database_url) or None,
    )


def create_engine_for(database_url: str) -> Engine:
    """Build a SQLAlchemy engine pointed at the shared application DB.

    SQLite URLs get `check_same_thread=False` (FastAPI's threadpool and
    DBOS workers hand connections across threads) and a driver-level
    `timeout` that acts as a busy handler. Persistent PRAGMAs (WAL,
    busy_timeout, foreign_keys) are applied by `_configure_sqlite_connection`
    for every SQLite connection in the process.
    """
    return create_engine(database_url, future=True, **_engine_kwargs(database_url))


def get_datasource() -> SQLAlchemyDatasource:
    """Return the process-wide datasource; raises if not yet initialized."""
    if _Holder.datasource is None:
        raise RuntimeError("datasource not initialized; call set_datasource() at startup")
    return _Holder.datasource


def get_engine() -> Engine:
    """Return the process-wide engine; raises if not yet initialized."""
    if _Holder.engine is None:
        raise RuntimeError("engine not initialized; call set_engine() at startup")
    return _Holder.engine


def set_datasource(datasource: SQLAlchemyDatasource) -> None:
    """Install the process-wide datasource. Call once from the entrypoint."""
    _Holder.datasource = datasource


def set_engine(engine: Engine) -> None:
    """Install the process-wide engine. Call once from the entrypoint."""
    _Holder.engine = engine


def sql_session() -> Session:
    """Open a new session against the shared engine.

    Callers own the transaction:

        with sql_session() as session, session.begin():
            ...
    """
    return Session(get_engine(), future=True)


def _engine_kwargs(database_url: str) -> dict[str, Any]:
    if not database_url.startswith("sqlite"):
        return {}
    return {"connect_args": {"check_same_thread": False, "timeout": 30}}


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_conn: Any, _record: Any) -> None:
    """Apply SQLite tuning to every connection across every Engine.

    DBOS builds its own system and datasource engines against our SQLite
    file; this global listener catches those connections too. WAL is
    persistent so re-issuing it per connect is a no-op; `busy_timeout`
    and `synchronous` are per-connection and must be set every time.

    `busy_timeout=30000` (30s) matches DBOS's own SQLite listener setting
    (see .venv/.../dbos/_sys_db_sqlite.py) so both engines wait the same
    length under lock contention — and complements the driver-level
    `timeout=30` we pass through `connect_args` in `_engine_kwargs`.
    """
    if not isinstance(dbapi_conn, sqlite3.Connection):
        return
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class _Holder:
    datasource: SQLAlchemyDatasource | None = None
    engine: Engine | None = None
