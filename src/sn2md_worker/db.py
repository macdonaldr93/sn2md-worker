from __future__ import annotations

from dbos import SQLAlchemyDatasource

__all__ = ["create_datasource", "get_datasource", "set_datasource"]


def create_datasource(database_url: str) -> SQLAlchemyDatasource:
    """Build a SQLAlchemyDatasource pointed at the shared application DB."""
    return SQLAlchemyDatasource.create(database_url)


def get_datasource() -> SQLAlchemyDatasource:
    """Return the process-wide datasource; raises if not yet initialized."""
    if _Holder.datasource is None:
        raise RuntimeError("datasource not initialized; call set_datasource() at startup")
    return _Holder.datasource


def set_datasource(datasource: SQLAlchemyDatasource) -> None:
    """Install the process-wide datasource. Call once from the entrypoint."""
    _Holder.datasource = datasource


class _Holder:
    datasource: SQLAlchemyDatasource | None = None
