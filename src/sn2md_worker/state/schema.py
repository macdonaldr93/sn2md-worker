from __future__ import annotations

from sn2md_worker.db import create_engine_for
from sn2md_worker.state.models import Base

__all__ = ["init_schema"]


def init_schema(database_url: str) -> None:
    """Create any missing application tables.

    `create_all` handles everything: no in-place migrations. Any schema
    change is applied on the next fresh install; the documented recovery
    path for a schema break is nuking `data/sn2md-worker.sqlite` and
    letting `backfill` re-populate from Drive. See CLAUDE.md item 3.
    """
    engine = create_engine_for(database_url)
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
