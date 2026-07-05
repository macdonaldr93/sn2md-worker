from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Dialect, Integer, MetaData, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "ConversionRecord",
    "ConversionStatus",
    "DebounceState",
    "DriveChangeCursor",
    "DriveWatchChannel",
    "PageConversion",
    "UTCDateTime",
]


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime that assumes/enforces UTC and re-attaches tzinfo on read.

    SQLite strips tzinfo when persisting `DateTime(timezone=True)`; this
    decorator normalizes both directions so callers always see aware
    datetimes.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class ConversionStatus:
    """String enum of `last_status` values on ConversionRecord."""

    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class ConversionRecord(Base):
    """One row per logical `.note` file (Drive path + filename)."""

    __tablename__ = "conversion_records"

    logical_key: Mapped[str] = mapped_column(String, primary_key=True)
    current_file_id: Mapped[str] = mapped_column(String, index=True)
    parent_folder_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_name: Mapped[str] = mapped_column(String)
    source_path: Mapped[str] = mapped_column(String)
    source_md5: Mapped[str | None] = mapped_column(String, nullable=True)
    output_rel_path: Mapped[str] = mapped_column(String)
    last_status: Mapped[str] = mapped_column(String)
    last_converted_at: Mapped[datetime] = mapped_column(UTCDateTime())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)


class DriveWatchChannel(Base):
    """A push-notification channel we've created against Drive changes.watch."""

    __tablename__ = "drive_watch_channels"

    channel_id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(String)
    webhook_url: Mapped[str | None] = mapped_column(String, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    start_page_token: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    is_active: Mapped[bool] = mapped_column(Boolean, index=True, default=False)


class DriveChangeCursor(Base):
    """Singleton row tracking the last-seen changes.list page token."""

    __tablename__ = "drive_change_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_token: Mapped[str] = mapped_column(String)
    last_polled_at: Mapped[datetime] = mapped_column(UTCDateTime())


class PageConversion(Base):
    """One row per converted `.note` page.

    Keyed on `(logical_key, page_index)` — used to skip Gemini calls when
    a page's rendered PNG hash matches what we've already converted.
    """

    __tablename__ = "page_conversions"

    logical_key: Mapped[str] = mapped_column(String, primary_key=True)
    page_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_md5: Mapped[str] = mapped_column(String)
    output_rel_path: Mapped[str] = mapped_column(String)
    last_converted_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DebounceState(Base):
    """Tracks in-flight debounce probes for a single Drive file id."""

    __tablename__ = "debounce_state"

    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    last_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_md5: Mapped[str | None] = mapped_column(String, nullable=True)
    stable_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime())
