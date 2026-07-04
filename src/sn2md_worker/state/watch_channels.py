from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sn2md_worker.state.models import DriveWatchChannel

__all__ = [
    "NewWatchChannel",
    "create",
    "get_active",
    "list_all",
    "mark_active",
]


@dataclass(frozen=True)
class NewWatchChannel:
    channel_id: str
    resource_id: str
    token: str
    expires_at: datetime
    start_page_token: str
    created_at: datetime


def create(session: Session, data: NewWatchChannel) -> DriveWatchChannel:
    channel = DriveWatchChannel(
        channel_id=data.channel_id,
        resource_id=data.resource_id,
        token=data.token,
        expires_at=data.expires_at,
        start_page_token=data.start_page_token,
        created_at=data.created_at,
        is_active=False,
    )
    session.add(channel)
    return channel


def get_active(session: Session) -> DriveWatchChannel | None:
    stmt = select(DriveWatchChannel).where(DriveWatchChannel.is_active.is_(True))
    return session.execute(stmt).scalars().first()


def list_all(session: Session) -> list[DriveWatchChannel]:
    stmt = select(DriveWatchChannel).order_by(DriveWatchChannel.created_at.desc())
    return list(session.execute(stmt).scalars())


def mark_active(session: Session, channel_id: str) -> None:
    """Set exactly one channel active, deactivating any others."""
    session.execute(
        update(DriveWatchChannel)
        .where(DriveWatchChannel.channel_id != channel_id)
        .values(is_active=False)
    )
    session.execute(
        update(DriveWatchChannel)
        .where(DriveWatchChannel.channel_id == channel_id)
        .values(is_active=True)
    )
