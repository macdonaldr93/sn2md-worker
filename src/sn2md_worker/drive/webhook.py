from __future__ import annotations

from dbos import DBOS
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
async def drive_webhook(request: Request) -> Response:
    """Receive a Google Drive push notification.

    Sync handshake: acknowledge without work. Real notifications are
    authenticated against `drive_watch_channels` (channel id + token)
    before enqueuing a `poll_changes` workflow.
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

    _log.info("drive_webhook_notification", **headers)
    DBOS.enqueue_workflow(POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_WEBHOOK)
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
    return channel.token == token


def _extract_goog_headers(request: Request) -> dict[str, str]:
    return {
        key[len(_GOOG_PREFIX) :].replace("-", "_"): value
        for key, value in request.headers.items()
        if key.lower().startswith(_GOOG_PREFIX)
    }
