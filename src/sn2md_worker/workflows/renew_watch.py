from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, DriveClientError, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import cursor, watch_channels
from sn2md_worker.state.models import DriveWatchChannel
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
    """Idempotent renewal check: create a new channel if none active,
    the current one expires within `RENEWAL_HEADROOM`, or it is
    registered to a URL that no longer matches settings.
    """
    with structlog.contextvars.bound_contextvars(
        workflow="renew_watch_channel", trigger=trigger_source
    ):
        _log.info("renew_watch_started")
        try:
            if not settings.webhook.url:
                _log.warning("renew_watch_skipped", reason="no_webhook_url")
                return

            with sql_session() as session:
                active = watch_channels.get_active(session)

            if active is not None:
                url_matches = active.webhook_url == settings.webhook.url
                still_fresh = (active.expires_at - now) > RENEWAL_HEADROOM
                if url_matches and still_fresh:
                    _log.info(
                        "renew_watch_skipped",
                        reason="still_fresh",
                        channel_id=active.channel_id,
                        expires_at=active.expires_at.isoformat(),
                    )
                    return
                if not url_matches:
                    _log.info(
                        "renew_watch_url_changed",
                        channel_id=active.channel_id,
                        old_url=active.webhook_url or "<unknown>",
                        new_url=settings.webhook.url,
                    )
                _try_stop_channel(drive=drive, active=active)

            _create_and_activate(drive=drive, settings=settings, now=now, trigger=trigger_source)
            _log.info("renew_watch_succeeded")
        except Exception as exc:
            _log.error("renew_watch_failed", error=str(exc), exc_info=True)
            raise


def ensure_active_channel(drive: DriveClient | None, settings: Settings) -> None:
    """Startup helper: seed the first channel if there isn't one."""
    if drive is None:
        _log.warning("renew_watch_skipped", trigger="startup", reason="no_drive_client")
        return
    renew_watch_channel_impl(
        trigger_source="startup",
        drive=drive,
        settings=settings,
        now=datetime.now(UTC),
    )


def _try_stop_channel(*, drive: DriveClient, active: DriveWatchChannel) -> None:
    """Best-effort — tell Drive to stop the old channel so it doesn't
    keep hitting a stale URL. Failures are logged, not fatal."""
    try:
        drive.stop_channel(active.channel_id, active.resource_id)
        _log.info("renew_watch_channel_stopped", channel_id=active.channel_id)
    except DriveClientError as exc:
        _log.warning(
            "renew_watch_channel_stop_failed",
            channel_id=active.channel_id,
            error=str(exc),
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
                webhook_url=settings.webhook.url,
                expires_at=info.expiration,
                start_page_token=page_token,
                created_at=now,
            ),
        )
        watch_channels.mark_active(session, info.id)

    _log.info(
        "renew_watch_channel_created",
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
