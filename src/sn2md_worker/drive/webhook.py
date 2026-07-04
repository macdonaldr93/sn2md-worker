from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from sn2md_worker.logging import get_logger

__all__ = ["router"]

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_GOOG_PREFIX = "x-goog-"
_SYNC_STATE = "sync"

_log = get_logger("sn2md_worker.drive.webhook")


@router.post("/drive", status_code=status.HTTP_200_OK)
async def drive_webhook(request: Request) -> Response:
    """Receive a Google Drive push notification.

    Returns 200 immediately. The sync handshake is a no-op; real change
    notifications will (M2) verify the channel token and enqueue a
    poll_changes workflow. For now we just log and acknowledge.
    """
    headers = _extract_goog_headers(request)
    if headers.get("resource_state") == _SYNC_STATE:
        _log.info("drive_webhook_sync", **headers)
    else:
        _log.info("drive_webhook_notification", **headers)
    return Response(status_code=status.HTTP_200_OK)


def _extract_goog_headers(request: Request) -> dict[str, str]:
    return {
        key[len(_GOOG_PREFIX) :].replace("-", "_"): value
        for key, value in request.headers.items()
        if key.lower().startswith(_GOOG_PREFIX)
    }
