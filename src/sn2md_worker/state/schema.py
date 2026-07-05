from __future__ import annotations

from sqlalchemy import Engine, create_engine, inspect

from sn2md_worker.state.models import Base

__all__ = ["init_schema"]


def init_schema(database_url: str) -> None:
    """Create any missing application tables and run tiny in-place upgrades.

    `create_all` handles fresh installs. Existing installs that predate a
    column addition get a targeted `ALTER TABLE ADD COLUMN` — see
    `_apply_micro_migrations`. This is our deliberate compromise instead
    of a full migration framework (see docs/product-brief).
    """
    engine = create_engine(database_url, future=True)
    try:
        Base.metadata.create_all(engine)
        _apply_micro_migrations(engine)
    finally:
        engine.dispose()


def _apply_micro_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    if inspector.has_table("drive_watch_channels"):
        columns = {c["name"] for c in inspector.get_columns("drive_watch_channels")}
        if "webhook_url" not in columns:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE drive_watch_channels ADD COLUMN webhook_url TEXT")
