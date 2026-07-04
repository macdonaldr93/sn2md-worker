from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import cursor, watch_channels
from sn2md_worker.state.watch_channels import NewWatchChannel

__all__ = [
    "RENEWAL_HEADROOM",
    "ensure_active_channel",
    "renew_watch_channel",
    "renew_watch_channel_impl",
]

RENEWAL_HEADROOM = timedelta(hours=24)

_log = get_logger("sn2md_worker.workflows.renew_watch")


@DBOS.workflow()
def renew_watch_channel(scheduled_time: datetime, context: str) -> None:
    """DBOS-scheduled wrapper that renews the watch channel if needed."""
    renew_watch_channel_impl(
        trigger_source=f"scheduled:{context}",
        drive=get_drive_client(),
        settings=get_settings(),
        now=datetime.now(UTC),
    )


def renew_watch_channel_impl(
    *,
    trigger_source: str,
    drive: DriveClient,
    settings: Settings,
    now: datetime,
) -> None:
    """Idempotent renewal check: create a new channel if none active or ≤24h left."""
    if not settings.webhook.url:
        _log.warning("renew_watch_skipped_no_webhook_url", trigger=trigger_source)
        return

    with sql_session() as session:
        active = watch_channels.get_active(session)
    if active is not None and (active.expires_at - now) > RENEWAL_HEADROOM:
        _log.info(
            "renew_watch_skipped_still_fresh",
            trigger=trigger_source,
            channel_id=active.channel_id,
            expires_at=active.expires_at.isoformat(),
        )
        return

    _create_and_activate(drive=drive, settings=settings, now=now, trigger=trigger_source)


def ensure_active_channel(drive: DriveClient | None, settings: Settings) -> None:
    """Startup helper: seed the first channel if there isn't one."""
    if drive is None:
        _log.warning("ensure_active_channel_skipped_no_drive_client")
        return
    renew_watch_channel_impl(
        trigger_source="startup",
        drive=drive,
        settings=settings,
        now=datetime.now(UTC),
    )


def _create_and_activate(
    *, drive: DriveClient, settings: Settings, now: datetime, trigger: str
) -> None:
    page_token = _current_or_fetch_page_token(drive)
    channel_id = uuid.uuid4().hex
    token = secrets.token_hex(16)
    ttl_seconds = settings.drive.watch_channel_ttl_days * 86_400

    info = drive.watch_changes(
        webhook_url=settings.webhook.url,
        channel_id=channel_id,
        token=token,
        start_page_token=page_token,
        ttl_seconds=ttl_seconds,
    )

    with sql_session() as session, session.begin():
        watch_channels.create(
            session,
            NewWatchChannel(
                channel_id=info.id,
                resource_id=info.resource_id,
                token=info.token,
                expires_at=info.expiration,
                start_page_token=page_token,
                created_at=now,
            ),
        )
        watch_channels.mark_active(session, info.id)

    _log.info(
        "renew_watch_created_channel",
        trigger=trigger,
        channel_id=info.id,
        resource_id=info.resource_id,
        expires_at=info.expiration.isoformat(),
    )


def _current_or_fetch_page_token(drive: DriveClient) -> str:
    with sql_session() as session:
        record = cursor.get(session)
        if record is not None:
            return record.page_token
    token = drive.get_start_page_token()
    with sql_session() as session, session.begin():
        cursor.set_cursor(session, token, datetime.now(UTC))
    return token
