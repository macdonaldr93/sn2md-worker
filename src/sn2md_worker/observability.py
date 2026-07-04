from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from sn2md_worker.config import get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.state import conversions, cursor, watch_channels
from sn2md_worker.state.models import (
    ConversionRecord,
    ConversionStatus,
    DriveChangeCursor,
    DriveWatchChannel,
)

__all__ = ["router"]


class HealthResponse(BaseModel):
    status: str


class ConversionSummary(BaseModel):
    logical_key: str
    file_id: str
    source_md5: str | None
    output_rel_path: str
    last_converted_at: datetime
    last_error: str | None = None


class WatchChannelSummary(BaseModel):
    channel_id: str | None
    resource_id: str | None
    expires_at: datetime | None
    is_active: bool


class CursorSummary(BaseModel):
    page_token: str | None
    last_polled_at: datetime | None


class StatusResponse(BaseModel):
    recent_conversions: list[ConversionSummary]
    recent_failures: list[ConversionSummary]
    watch_channel: WatchChannelSummary
    change_cursor: CursorSummary


router = APIRouter(tags=["observability"])


@router.get("/healthz", status_code=status.HTTP_200_OK, response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness probe — the process is up and serving HTTP."""
    return HealthResponse(status="ok")


@router.get("/readyz", responses={200: {"model": HealthResponse}, 503: {}})
async def readyz() -> Response:
    """Readiness probe.

    - Dev mode (`webhook.url` empty): 200 as long as the app is up.
    - Prod mode: 200 only if an active `drive_watch_channels` row exists
      with `expires_at > now`; 503 otherwise.
    """
    settings = get_settings()
    if not settings.webhook.url:
        return Response(status_code=status.HTTP_200_OK)

    try:
        with sql_session() as session:
            channel = watch_channels.get_active(session)
    except RuntimeError:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    if channel is None or channel.expires_at < datetime.now(UTC):
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_200_OK)


@router.get("/status", response_model=StatusResponse)
async def get_status() -> StatusResponse:
    """Operational snapshot: recent conversions/failures, watch channel, cursor."""
    settings = get_settings()
    if not settings.observability.status_endpoint_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    with sql_session() as session:
        recent_success = conversions.list_recent_by_status(
            session, status=ConversionStatus.SUCCESS, limit=20
        )
        recent_error = conversions.list_recent_by_status(
            session, status=ConversionStatus.ERROR, limit=20
        )
        active_channel = watch_channels.get_active(session)
        change_cursor = cursor.get(session)

    return StatusResponse(
        recent_conversions=[_to_summary(record) for record in recent_success],
        recent_failures=[_to_summary(record) for record in recent_error],
        watch_channel=_channel_summary(active_channel),
        change_cursor=_cursor_summary(change_cursor),
    )


def _to_summary(record: ConversionRecord) -> ConversionSummary:
    return ConversionSummary(
        logical_key=record.logical_key,
        file_id=record.current_file_id,
        source_md5=record.source_md5,
        output_rel_path=record.output_rel_path,
        last_converted_at=record.last_converted_at,
        last_error=record.last_error,
    )


def _channel_summary(channel: DriveWatchChannel | None) -> WatchChannelSummary:
    if channel is None:
        return WatchChannelSummary(
            channel_id=None, resource_id=None, expires_at=None, is_active=False
        )
    return WatchChannelSummary(
        channel_id=channel.channel_id,
        resource_id=channel.resource_id,
        expires_at=channel.expires_at,
        is_active=True,
    )


def _cursor_summary(record: DriveChangeCursor | None) -> CursorSummary:
    if record is None:
        return CursorSummary(page_token=None, last_polled_at=None)
    return CursorSummary(
        page_token=record.page_token,
        last_polled_at=record.last_polled_at,
    )
