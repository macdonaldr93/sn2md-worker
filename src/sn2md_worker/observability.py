from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import bindparam, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from sn2md_worker.clock import now_utc
from sn2md_worker.config import get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.startup_status import StepStatus, get_startup_status
from sn2md_worker.state import conversions, cursor, watch_channels
from sn2md_worker.state.conversions import ConversionRecordView
from sn2md_worker.state.cursor import CursorView
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.state.watch_channels import WatchChannelView

__all__ = ["router"]

_QUEUE_NAMES = ("convert_queue", "poll_queue")
_TERMINAL_STATUSES = (
    "SUCCESS",
    "ERROR",
    "CANCELLED",
    "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
)


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


class QueueDepth(BaseModel):
    """In-flight workflow counts per DBOS queue."""

    convert_queue: int = 0
    poll_queue: int = 0


class BackfillStatus(BaseModel):
    """Latest backfill workflow observed in DBOS's workflow_status table."""

    status: str | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None


class StartupSnapshot(BaseModel):
    """Outcome of each boot-time init step.

    Each field is `"ok"` | `"deferred"` | `"failed"`. `deferred` means
    the step was intentionally skipped (typically because DriveClient
    isn't configured — the dev-mode path). `failed` means the step
    tried and errored; grep the container logs for `boot_step_failed`
    or `drive_client_init_failed` for the traceback. When no startup
    has been recorded yet (early boot, tests without the full
    entrypoint), every field is `"deferred"` and `last_error` is
    `null`.
    """

    drive_client: StepStatus = "deferred"
    seed_cursor: StepStatus = "deferred"
    ensure_channel: StepStatus = "deferred"
    backfill_enqueue: StepStatus = "deferred"
    last_error: str | None = None


class StatusResponse(BaseModel):
    recent_conversions: list[ConversionSummary]
    recent_failures: list[ConversionSummary]
    # In-flight + crash-stuck converts (row lingering here = didn't finish).
    recent_pending: list[ConversionSummary]
    watch_channel: WatchChannelSummary
    change_cursor: CursorSummary
    queue_depth: QueueDepth
    backfill: BackfillStatus
    startup: StartupSnapshot


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

    if channel is None:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    # Belt-and-suspenders vs. tz-naive datetimes: `UTCDateTime` re-attaches
    # UTC on read, so the row we just loaded is already aware. If a future
    # schema change ever drops that, comparing against `now_utc()`
    # would raise `TypeError`; normalizing here keeps `/readyz` behaving
    # (returning 503 for expired) instead of 500-ing.
    expires = (
        channel.expires_at
        if channel.expires_at.tzinfo is not None
        else channel.expires_at.replace(tzinfo=UTC)
    )
    if expires < now_utc():
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
        recent_pending = conversions.list_recent_by_status(
            session, status=ConversionStatus.PENDING, limit=20
        )
        active_channel = watch_channels.get_active(session)
        change_cursor = cursor.get(session)
        queue_depth = _query_queue_depth(session)
        backfill = _query_backfill_status(session)

    return StatusResponse(
        recent_conversions=[_to_summary(record) for record in recent_success],
        recent_failures=[_to_summary(record) for record in recent_error],
        recent_pending=[_to_summary(record) for record in recent_pending],
        watch_channel=_channel_summary(active_channel),
        change_cursor=_cursor_summary(change_cursor),
        queue_depth=queue_depth,
        backfill=backfill,
        startup=_startup_snapshot(),
    )


def _startup_snapshot() -> StartupSnapshot:
    """Render the process-wide StartupStatus, or a fully-deferred default."""
    status = get_startup_status()
    if status is None:
        return StartupSnapshot()
    return StartupSnapshot(
        drive_client=status.drive_client,
        seed_cursor=status.seed_cursor,
        ensure_channel=status.ensure_channel,
        backfill_enqueue=status.backfill_enqueue,
        last_error=status.last_error,
    )


def _query_queue_depth(session: Session) -> QueueDepth:
    """Count non-terminal workflows per queue in DBOS's workflow_status.

    Reads DBOS's own table via raw SQL — coupling we accept for
    observability. If the table isn't present yet (tests without a full
    DBOS init, or a very early boot), we return the zero default.

    `_TERMINAL_STATUSES` is bound via an expanding parameter rather than
    f-string interpolation so the query can never grow an injection
    vector if the constant is ever built from user-controlled input.
    """
    stmt = text(
        "SELECT queue_name, COUNT(*) AS cnt FROM workflow_status "
        "WHERE queue_name IS NOT NULL AND status NOT IN :terminal "
        "GROUP BY queue_name"
    ).bindparams(bindparam("terminal", expanding=True))
    try:
        rows = session.execute(stmt, {"terminal": list(_TERMINAL_STATUSES)}).all()
    except DatabaseError:
        return QueueDepth()

    counts = {row.queue_name: int(row.cnt) for row in rows}
    return QueueDepth(
        convert_queue=counts.get("convert_queue", 0),
        poll_queue=counts.get("poll_queue", 0),
    )


def _query_backfill_status(session: Session) -> BackfillStatus:
    """Return the latest backfill workflow's outcome from workflow_status."""
    stmt = text(
        "SELECT status, started_at_epoch_ms, completed_at, error "
        "FROM workflow_status "
        "WHERE name = 'backfill' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    try:
        row = session.execute(stmt).first()
    except DatabaseError:
        row = None
    if row is None:
        return BackfillStatus(status=None, started_at=None, completed_at=None, error=None)
    return BackfillStatus(
        status=row.status,
        started_at=_from_epoch_ms(row.started_at_epoch_ms),
        completed_at=_from_epoch_ms(row.completed_at),
        error=row.error,
    )


def _from_epoch_ms(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _to_summary(record: ConversionRecordView) -> ConversionSummary:
    return ConversionSummary(
        logical_key=record.logical_key,
        file_id=record.current_file_id,
        source_md5=record.source_md5,
        output_rel_path=record.output_rel_path,
        last_converted_at=record.last_converted_at,
        last_error=record.last_error,
    )


def _channel_summary(channel: WatchChannelView | None) -> WatchChannelSummary:
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


def _cursor_summary(record: CursorView | None) -> CursorSummary:
    if record is None:
        return CursorSummary(page_token=None, last_polled_at=None)
    return CursorSummary(
        page_token=record.page_token,
        last_polled_at=record.last_polled_at,
    )
