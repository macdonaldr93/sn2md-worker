from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

import structlog
from dbos import DBOS

from sn2md_worker.clock import now_utc
from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, DriveClientError, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import cursor, watch_channels
from sn2md_worker.state.watch_channels import NewWatchChannel, WatchChannelView
from sn2md_worker.workflows.poll_changes import poll_changes
from sn2md_worker.workflows.queues import POLL_QUEUE_NAME

__all__ = [
    "RENEWAL_HEADROOM",
    "ensure_active_channel",
    "renew_watch_channel",
    "renew_watch_channel_impl",
]

# 48h headroom against the daily 06:00 UTC cron: a channel is still
# eligible for renewal on the run before its final day, so a single
# missed cron (container down for a day) doesn't leave the channel
# expiring silently. Combined with the startup recovery poll below,
# a missed-notifications window is bounded even without a fallback
# poll_changes cron.
RENEWAL_HEADROOM = timedelta(hours=48)

_log = get_logger("sn2md_worker.workflows.renew_watch")


@DBOS.workflow()
def renew_watch_channel(scheduled_time: datetime, context: str) -> None:
    renew_watch_channel_impl(
        trigger_source=f"scheduled:{context}",
        drive=get_drive_client(),
        settings=get_settings(),
        now=now_utc(),
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
            if active is not None:
                # We just replaced an existing channel. Enqueue a catch-up
                # poll to cover the seam between the old channel's last
                # delivered push and the new channel becoming active.
                # First-ever channel creation (active is None) needs no
                # catch-up: the startup backfill covers the initial state.
                # At boot with an already-expired channel,
                # ensure_active_channel also enqueues a "recovery" poll, so
                # this path can enqueue a second poll then - harmless, since
                # poll_queue runs at concurrency 1 and the follow-up poll is
                # a cheap no-op once the cursor has advanced.
                DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, "renewal")
            _log.info("renew_watch_succeeded")
        except Exception as exc:
            _log.error("renew_watch_failed", error=str(exc), exc_info=True)
            raise


def ensure_active_channel(drive: DriveClient | None, settings: Settings) -> None:
    """Startup helper: seed the first channel if there isn't one.

    If the previously-active channel has already expired at boot, also
    enqueues a `poll_changes("recovery")` to catch up on notifications
    Drive sent while our webhook wasn't listening — needed because we
    don't run a fallback `poll_changes` cron. The recovery poll uses
    whatever `drive_change_cursor` we last persisted, so it walks from
    the last confirmed point rather than the current head.
    """
    if drive is None:
        _log.warning("renew_watch_skipped", trigger="startup", reason="no_drive_client")
        return

    now = now_utc()
    with sql_session() as session:
        active = watch_channels.get_active(session)

    if active is not None and active.expires_at <= now:
        _log.warning(
            "renew_watch_previous_channel_expired",
            channel_id=active.channel_id,
            expired_at=active.expires_at.isoformat(),
        )
        DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, "recovery")

    renew_watch_channel_impl(
        trigger_source="startup",
        drive=drive,
        settings=settings,
        now=now,
    )


def _try_stop_channel(*, drive: DriveClient, active: WatchChannelView) -> None:
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
    """Two-phase channel creation.

    Phase 1: pre-persist the row (channel_id + token + placeholder
    resource_id/expires_at) BEFORE calling Drive. If we crash between
    Drive's `changes.watch` succeeding and our DB commit, the row still
    exists on our side, so incoming webhook pushes for the crashed
    channel can still authenticate (channel_id + token match) — the
    orphan-on-Drive risk isn't fully eliminated (Drive doesn't expose a
    "list my channels" API), but at least notifications aren't lost
    silently. The placeholder `expires_at` is `now + 7 days` (Drive's
    default TTL) so `drive_webhook._authenticate`'s expiry check
    doesn't reject pushes arriving between phase 1 and phase 2.

    Phase 2: `confirm` writes back Drive's real `resource_id` and
    `expires_at`, then `mark_active` promotes the row. On a Drive-side
    failure the pending row is rolled back so it doesn't confuse the
    next renewal.
    """
    page_token = _current_or_fetch_page_token(drive)
    channel_id = uuid.uuid4().hex
    token = secrets.token_hex(16)

    with sql_session() as session, session.begin():
        watch_channels.create(
            session,
            NewWatchChannel(
                channel_id=channel_id,
                # Placeholders — `confirm` overwrites both once Drive replies.
                # A row in this state is "pending": auth-usable but not yet
                # promoted via `mark_active`. `expires_at` matches Drive's
                # default 7-day TTL so webhook auth accepts pushes during
                # the phase-1→phase-2 window.
                resource_id="",
                token=token,
                webhook_url=settings.webhook.url,
                expires_at=now + timedelta(days=7),
                start_page_token=page_token,
                created_at=now,
            ),
        )

    try:
        info = drive.watch_changes(
            webhook_url=settings.webhook.url,
            channel_id=channel_id,
            token=token,
            start_page_token=page_token,
        )
    except Exception:
        # Drive rejected the request — roll back the pending row so it
        # doesn't confuse future renewals into thinking a channel exists.
        with sql_session() as session, session.begin():
            watch_channels.delete_by_id(session, channel_id)
        raise

    with sql_session() as session, session.begin():
        # Confirm and promote using OUR channel_id, not `info.id` — Drive
        # echoes it back, but the pending row we just wrote is keyed on
        # what we generated, and trusting our local id survives a
        # (theoretical) Drive-side echo bug.
        watch_channels.confirm(
            session,
            channel_id=channel_id,
            resource_id=info.resource_id,
            expires_at=info.expiration,
        )
        watch_channels.mark_active(session, channel_id)

    _log.info(
        "renew_watch_channel_created",
        trigger=trigger,
        channel_id=channel_id,
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
        cursor.set_cursor(session, token, now_utc())
    return token
