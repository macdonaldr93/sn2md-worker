from __future__ import annotations

import hmac
from datetime import UTC, datetime

from dbos import DBOS, SetEnqueueOptions
from dbos._error import DBOSQueueDeduplicatedError  # noqa: PLC2701
from fastapi import APIRouter, Request, Response, status

from sn2md_worker.db import sql_session
from sn2md_worker.logging import get_logger
from sn2md_worker.state.models import DriveWatchChannel
from sn2md_worker.workflows import POLL_QUEUE_NAME
from sn2md_worker.workflows.poll_changes import poll_changes

__all__ = ["POLL_TRIGGER_WEBHOOK", "router"]

POLL_TRIGGER_WEBHOOK = "webhook"

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_GOOG_PREFIX = "x-goog-"
_SYNC_STATE = "sync"

_log = get_logger("sn2md_worker.drive.webhook")


@router.post("/drive", status_code=status.HTTP_200_OK)
def drive_webhook(request: Request) -> Response:
    """Receive a Google Drive push notification.

    Sync route by design: the body does blocking SQLA + DBOS work.
    FastAPI runs sync routes in its threadpool so the event loop stays
    responsive to other requests. Sync handshake: ack without work. Real
    notifications are authenticated against `drive_watch_channels`
    (channel id + token + not-expired) before enqueueing a `poll_changes`
    workflow. DBOS `deduplication_id` keyed on `(channel_id, message_number)`
    drops Google's retries of the same push.
    """
    headers = _extract_goog_headers(request)
    if headers.get("resource_state") == _SYNC_STATE:
        _log.info("drive_webhook_sync", **headers)
        return Response(status_code=status.HTTP_200_OK)

    channel_id = headers.get("channel_id", "")
    token = headers.get("channel_token", "")
    if not _authenticate(channel_id=channel_id, token=token):
        _log.warning("drive_webhook_unauthenticated", **headers)
        return Response(status_code=status.HTTP_200_OK)

    message_number = headers.get("message_number", "")
    dedup_id = f"{channel_id}:{message_number}" if message_number else None

    try:
        if dedup_id is not None:
            with SetEnqueueOptions(deduplication_id=dedup_id):
                DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_WEBHOOK)
        else:
            DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_WEBHOOK)
    except DBOSQueueDeduplicatedError:
        # Google's push-retry hit us with an already-active dedup id — the
        # first delivery is doing (or has queued) the poll_changes. Ack.
        _log.info("drive_webhook_deduped", dedup_id=dedup_id, **headers)
        return Response(status_code=status.HTTP_200_OK)

    _log.info("drive_webhook_notification", **headers)
    return Response(status_code=status.HTTP_200_OK)


def _authenticate(*, channel_id: str, token: str) -> bool:
    if not channel_id or not token:
        return False
    try:
        with sql_session() as session:
            channel = session.get(DriveWatchChannel, channel_id)
    except RuntimeError:
        return False
    if channel is None:
        return False
    if channel.expires_at <= datetime.now(UTC):
        return False
    # Constant-time compare so the shared secret can't be probed via
    # response-timing on the not-matching branch.
    return hmac.compare_digest(channel.token, token)


def _extract_goog_headers(request: Request) -> dict[str, str]:
    return {
        key[len(_GOOG_PREFIX) :].replace("-", "_"): value
        for key, value in request.headers.items()
        if key.lower().startswith(_GOOG_PREFIX)
    }
