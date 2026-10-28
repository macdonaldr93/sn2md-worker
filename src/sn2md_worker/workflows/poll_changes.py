from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import structlog
from dbos import DBOS, SetEnqueueOptions
from dbos._error import DBOSQueueDeduplicatedError
from pydantic import BaseModel, ConfigDict

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.correlation import new_correlation_id
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, DrivePermanentError, get_drive_client
from sn2md_worker.drive.models import ChangeEvent
from sn2md_worker.drive.paths import resolve_source_path
from sn2md_worker.logging import get_logger
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.state import conversions, cursor
from sn2md_worker.workflows.backfill import backfill
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output
from sn2md_worker.workflows.queues import (
    CONVERT_QUEUE_NAME,
    DELETE_QUEUE_NAME,
    POLL_QUEUE_NAME,
)

__all__ = [
    "CONVERT_QUEUE_NAME",
    "DELETE_QUEUE_NAME",
    "POLL_TRIGGER_FALLBACK",
    "poll_changes",
    "poll_changes_impl",
    "scheduled_poll_changes",
    "seed_cursor",
]

_NOTE_EXTENSION = ".note"
POLL_TRIGGER_FALLBACK = "fallback"

_log = get_logger("sn2md_worker.workflows.poll_changes")


class _ChangeAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: Literal["convert_note", "delete_output"]
    file_id: str
    source_path: str | None = None
    reason: str | None = None


class _PagePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    cursor_expired: bool = False
    new_cursor: str | None = None
    actions: tuple[_ChangeAction, ...] = ()
    ignored: int = 0
    errored: int = 0
    next_page_token: str | None = None
    new_start_page_token: str | None = None


@DBOS.workflow()
def poll_changes(trigger_source: str, correlation_id: str | None = None) -> None:
    with structlog.contextvars.bound_contextvars(dbos_workflow_id=DBOS.workflow_id):
        poll_changes_impl(
            trigger_source=trigger_source,
            drive=get_drive_client(),
            settings=get_settings(),
            correlation_id=correlation_id,
        )


@DBOS.workflow()
def scheduled_poll_changes(scheduled_time: datetime, context: str) -> None:
    """DBOS-scheduled fallback. Enqueue a poll onto poll_queue (rather
    than running inline) so it serializes behind any webhook-triggered
    poll instead of racing it on the shared cursor. The safety net for
    push notifications Google dropped while the process stayed up.

    Signature stays exactly `(scheduled_time, context)`: that is what
    `DBOS.create_schedule` invokes scheduled workflows with. Root
    trigger: a fresh correlation id is minted per cron tick."""
    DBOS.enqueue_workflow(
        POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_FALLBACK, new_correlation_id()
    )


def poll_changes_impl(
    *,
    trigger_source: str,
    drive: DriveClient,
    settings: Settings,
    correlation_id: str | None = None,
) -> None:
    """Walk changes since the last saved cursor and enqueue conversions.

    Cursor advances **per page** rather than once at the end. If the walk
    dies mid-page (Drive down, DBOS unavailable), the cursor stays at
    the start of the failing page and DBOS retries pick up there instead
    of re-walking every page since the pre-crash token. Per-change
    dispatch errors are logged and skipped — a change for a since-deleted
    file that surfaces as a permanent 404 shouldn't stall every later
    change on the same page.
    """
    # Self-healing mint: replays of pre-upgrade enqueues (and direct
    # invocations) arrive with None and still get a usable id.
    correlation_id = correlation_id or new_correlation_id()
    with structlog.contextvars.bound_contextvars(
        workflow="poll_changes", trigger=trigger_source, correlation_id=correlation_id
    ):
        _log.info("poll_changes_started")
        try:
            page_token = _load_or_init_cursor(drive)
            enqueued = 0
            ignored = 0
            errored = 0

            # `resolve_source_path` walks a file's parent chain; sibling
            # changes on the same page usually share several ancestors, so
            # one cache pays for the whole walk. Fresh per run so a rename
            # doesn't get served a stale entry on the next poll.
            metadata_cache: dict[str, NoteMetadata] = {}

            while True:
                plan = _fetch_page_plan(
                    page_token=page_token,
                    drive=drive,
                    settings=settings,
                    metadata_cache=metadata_cache,
                )
                if plan.cursor_expired:
                    DBOS.enqueue_workflow(POLL_QUEUE_NAME, backfill, correlation_id)
                    _log.info(
                        "poll_changes_recovery_enqueued",
                        new_cursor=plan.new_cursor,
                    )
                    return

                ignored += plan.ignored
                errored += plan.errored
                for action in plan.actions:
                    if _enqueue_action(action, correlation_id):
                        enqueued += 1
                    else:
                        ignored += 1

                if plan.next_page_token:
                    page_token = plan.next_page_token
                    _save_cursor(page_token)
                    continue
                if plan.new_start_page_token:
                    page_token = plan.new_start_page_token
                    _save_cursor(page_token)
                break

            _log.info(
                "poll_changes_succeeded",
                enqueued=enqueued,
                ignored=ignored,
                errored=errored,
                cursor=page_token,
            )
        except Exception as exc:
            _log.error("poll_changes_failed", error=str(exc), exc_info=True)
            raise


def seed_cursor(drive: DriveClient) -> None:
    """Ensure `drive_change_cursor` has a value; safe to call every boot."""
    with sql_session() as session:
        if cursor.get(session) is not None:
            return
    token = drive.get_start_page_token()
    with sql_session() as session, session.begin():
        seeded = cursor.seed_if_empty(session, token, datetime.now(UTC))
    _log.info("cursor_seeded", token=seeded)


@DBOS.step()
def _fetch_page_plan(
    *,
    page_token: str,
    drive: DriveClient,
    settings: Settings,
    metadata_cache: dict[str, NoteMetadata],
) -> _PagePlan:
    """Fetch one Drive changes page and resolve per-change dispatch actions."""

    def cached_get_metadata(fid: str) -> NoteMetadata:
        if fid not in metadata_cache:
            metadata_cache[fid] = drive.get_metadata(fid, fields="id,name,parents")
        return metadata_cache[fid]

    try:
        page = drive.changes_list(page_token=page_token)
    except DrivePermanentError as exc:
        # A 4xx on `changes.list` almost always means our persisted cursor
        # is older than Drive's change-log window (docs: tokens more than a
        # few weeks old can 404). Reset from `getStartPageToken` and let the
        # enclosing workflow enqueue a backfill.
        _log.warning(
            "poll_changes_cursor_expired",
            cursor=page_token,
            error=str(exc),
        )
        new_token = drive.get_start_page_token()
        _save_cursor_row(new_token)
        return _PagePlan(
            cursor_expired=True,
            new_cursor=new_token,
            actions=(),
            ignored=0,
            errored=0,
            next_page_token=None,
            new_start_page_token=None,
        )

    actions: list[_ChangeAction] = []
    ignored = 0
    errored = 0
    for change in page.changes:
        try:
            action = _resolve_change_action(change, cached_get_metadata, settings)
        except DrivePermanentError as exc:
            # 4xx from Drive: the file is gone / rejected / forbidden.
            # No retry can fix that, so log it and keep processing
            # the rest of the page. Transient errors propagate out of the
            # step and stall the workflow so DBOS retries from the last
            # saved cursor.
            _log.warning(
                "poll_changes_dispatch_failed",
                file_id=change.file_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            errored += 1
            continue
        if action is None:
            ignored += 1
        else:
            actions.append(action)

    return _PagePlan(
        cursor_expired=False,
        new_cursor=None,
        actions=tuple(actions),
        ignored=ignored,
        errored=errored,
        next_page_token=page.next_page_token,
        new_start_page_token=page.new_start_page_token,
    )


def _resolve_change_action(
    change: ChangeEvent,
    get_metadata: Callable[[str], NoteMetadata],
    settings: Settings,
) -> _ChangeAction | None:
    if change.removed or (change.file is not None and change.file.trashed):
        reason = "removed" if change.removed else "trashed"
        return _ChangeAction(
            target="delete_output",
            file_id=change.file_id,
            reason=reason,
        )

    source_path: str | None = None
    if change.file is not None and change.file.name.lower().endswith(_NOTE_EXTENSION):
        source_path = resolve_source_path(
            file_id=change.file_id,
            root_folder_id=settings.drive.source_folder_id,
            get_metadata=get_metadata,
        )
    if source_path is None:
        with sql_session() as session:
            existing = conversions.get_by_current_file_id(session, change.file_id)
        if existing is None:
            return None
        return _ChangeAction(
            target="delete_output",
            file_id=change.file_id,
            reason="moved_outside_source_folder",
        )

    return _ChangeAction(
        target="convert_note",
        file_id=change.file_id,
        source_path=source_path,
    )


def _enqueue_action(action: _ChangeAction, correlation_id: str) -> bool:
    """Enqueue convert_note or delete_output from the workflow orchestration loop."""
    if action.target == "delete_output":
        DBOS.enqueue_workflow(DELETE_QUEUE_NAME, delete_output, action.file_id, correlation_id)
        _log.info(
            "poll_changes_enqueued",
            file_id=action.file_id,
            target="delete_output",
            reason=action.reason,
        )
        return True

    assert action.source_path is not None
    try:
        with SetEnqueueOptions(deduplication_id=f"{action.file_id}:{action.source_path}"):
            DBOS.enqueue_workflow(
                CONVERT_QUEUE_NAME,
                convert_note,
                action.file_id,
                action.source_path,
                correlation_id,
            )
    except DBOSQueueDeduplicatedError:
        _log.info(
            "poll_changes_convert_deduplicated",
            file_id=action.file_id,
            source_path=action.source_path,
        )
        return False
    _log.info(
        "poll_changes_enqueued",
        file_id=action.file_id,
        target="convert_note",
        source_path=action.source_path,
    )
    return True


@DBOS.step()
def _load_or_init_cursor(drive: DriveClient) -> str:
    with sql_session() as session:
        record = cursor.get(session)
        if record is not None:
            return record.page_token
    token = drive.get_start_page_token()
    with sql_session() as session, session.begin():
        return cursor.seed_if_empty(session, token, datetime.now(UTC))


@DBOS.step()
def _save_cursor(page_token: str) -> None:
    _save_cursor_row(page_token)


def _save_cursor_row(page_token: str) -> None:
    with sql_session() as session, session.begin():
        cursor.set_cursor(session, page_token, datetime.now(UTC))
