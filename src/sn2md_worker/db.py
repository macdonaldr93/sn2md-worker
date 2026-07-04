from __future__ import annotations

from dbos import SQLAlchemyDatasource
from sqlalchemy import Engine, create_engine
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
    """Build a SQLAlchemyDatasource pointed at the shared application DB."""
    return SQLAlchemyDatasource.create(database_url)


def create_engine_for(database_url: str) -> Engine:
    """Build a SQLAlchemy engine pointed at the shared application DB."""
    return create_engine(database_url, future=True)


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


class _Holder:
    datasource: SQLAlchemyDatasource | None = None
    engine: Engine | None = None
