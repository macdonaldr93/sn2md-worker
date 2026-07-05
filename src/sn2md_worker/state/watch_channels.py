from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, delete, select, update
from sqlalchemy.orm import Session

from sn2md_worker.state.models import DriveWatchChannel

__all__ = [
    "NewWatchChannel",
    "WatchChannelView",
    "confirm",
    "create",
    "delete_by_id",
    "get_active",
    "list_all",
    "mark_active",
]


@dataclass(frozen=True)
class NewWatchChannel:
    channel_id: str
    resource_id: str
    token: str
    webhook_url: str
    expires_at: datetime
    start_page_token: str
    created_at: datetime


@dataclass(frozen=True)
class WatchChannelView:
    """Read-only snapshot of a `drive_watch_channels` row."""

    channel_id: str
    resource_id: str
    token: str
    webhook_url: str | None
    expires_at: datetime
    start_page_token: str
    created_at: datetime
    is_active: bool


def create(session: Session, data: NewWatchChannel) -> None:
    channel = DriveWatchChannel(
        channel_id=data.channel_id,
        resource_id=data.resource_id,
        token=data.token,
        webhook_url=data.webhook_url,
        expires_at=data.expires_at,
        start_page_token=data.start_page_token,
        created_at=data.created_at,
        is_active=False,
    )
    session.add(channel)


def get_active(session: Session) -> WatchChannelView | None:
    stmt = select(DriveWatchChannel).where(DriveWatchChannel.is_active.is_(True))
    record = session.execute(stmt).scalars().first()
    return _to_view(record) if record is not None else None


def list_all(session: Session) -> list[WatchChannelView]:
    stmt = select(DriveWatchChannel).order_by(DriveWatchChannel.created_at.desc())
    return [_to_view(row) for row in session.execute(stmt).scalars()]


def _to_view(record: DriveWatchChannel) -> WatchChannelView:
    return WatchChannelView(
        channel_id=record.channel_id,
        resource_id=record.resource_id,
        token=record.token,
        webhook_url=record.webhook_url,
        expires_at=record.expires_at,
        start_page_token=record.start_page_token,
        created_at=record.created_at,
        is_active=record.is_active,
    )


def confirm(
    session: Session,
    *,
    channel_id: str,
    resource_id: str,
    expires_at: datetime,
) -> None:
    """Fill in the Drive-side values on a previously-`create`d pending row.

    Used by the renew_watch two-phase flow: `create` writes the row
    before the Drive `changes.watch` HTTP call (so the channel_id+token
    survive a crash between the two), then `confirm` writes back the
    real `resource_id` + `expires_at` returned by Drive.
    """
    session.execute(
        update(DriveWatchChannel)
        .where(DriveWatchChannel.channel_id == channel_id)
        .values(resource_id=resource_id, expires_at=expires_at)
    )


def delete_by_id(session: Session, channel_id: str) -> None:
    """Remove a channel row (used to roll back a failed Drive call)."""
    session.execute(delete(DriveWatchChannel).where(DriveWatchChannel.channel_id == channel_id))


def mark_active(session: Session, channel_id: str) -> None:
    """Set exactly one channel active, deactivating any others.

    One statement instead of two so no reader can observe a moment where
    zero channels are active mid-transition.
    """
    session.execute(
        update(DriveWatchChannel).values(
            is_active=case(
                (DriveWatchChannel.channel_id == channel_id, True),
                else_=False,
            )
        )
    )
