from __future__ import annotations

from sqlalchemy import create_engine

from sn2md_worker.state.models import Base

__all__ = ["init_schema"]


def init_schema(database_url: str) -> None:
    """Create any missing application tables. Idempotent."""
    engine = create_engine(database_url, future=True)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
