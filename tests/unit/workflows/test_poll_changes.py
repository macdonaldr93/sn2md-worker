"""BDD-style tests for poll_changes_impl.

The workflow's contract is a behavior: given a set of Drive changes and
a cursor, it decides what to enqueue and how to advance the cursor.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import DriveConfig, Settings
from sn2md_worker.db import set_engine
from sn2md_worker.drive.client import (
    DriveClient,
    DrivePermanentError,
    DriveTransientError,
)
from sn2md_worker.drive.models import ChangeEvent, ChangesPage, FileMetadata
from sn2md_worker.state import cursor
from sn2md_worker.state.models import Base
from sn2md_worker.workflows.backfill import backfill
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output
from sn2md_worker.workflows.poll_changes import (
    CONVERT_QUEUE_NAME,
    DELETE_QUEUE_NAME,
    poll_changes_impl,
    seed_cursor,
)

SOURCE_FOLDER_ID = "SRC"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'poll.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(drive=DriveConfig(source_folder_id=SOURCE_FOLDER_ID))


@pytest.fixture
def drive() -> MagicMock:
    return MagicMock(spec=DriveClient)


def _change(
    file_id: str, *, name: str, parents: tuple[str, ...] = (SOURCE_FOLDER_ID,)
) -> ChangeEvent:
    return ChangeEvent(
        fileId=file_id,
        removed=False,
        file=FileMetadata(id=file_id, name=name, parents=parents),
    )


def _removal(file_id: str) -> ChangeEvent:
    return ChangeEvent(fileId=file_id, removed=True)


class TestWhenChangesContainNoteUpdates:
    def test_enqueues_each_note_and_saves_cursor(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — the changes feed contains two .note updates in the source folder
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="2026-07.note"),
                _change("file-2", name="2026-08.note"),
            ),
            new_start_page_token="42",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: FileMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )

        # WHEN — poll_changes_impl runs
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — both files are enqueued to the convert queue
        assert enqueue.call_count == 2
        first_call = enqueue.call_args_list[0]
        assert first_call.args[0] == CONVERT_QUEUE_NAME
        assert first_call.args[1] is convert_note
        assert first_call.args[2] == "file-1"

        # AND — the cursor advances to the returned new_start_page_token
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "42"


class TestWhenChangesContainNonNoteFiles:
    def test_non_note_files_are_ignored(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a mix of note and non-note changes
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="README.md"),
                _change("file-2", name="notes.note"),
            ),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: FileMetadata(
            id=fid, name="notes.note", parents=(SOURCE_FOLDER_ID,)
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — only the .note file is enqueued
        assert enqueue.call_count == 1
        assert enqueue.call_args.args[2] == "file-2"


class TestWhenChangesContainRemovalsOrTrashedFiles:
    def test_removals_enqueue_delete_output(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a removal event
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_removal("file-1"),),
            new_start_page_token="10",
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — delete_output enqueued to its own queue (not convert)
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == DELETE_QUEUE_NAME
        assert args[1] is delete_output
        assert args[2] == "file-1"

    def test_trashed_files_enqueue_delete_output(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a change reporting a trashed file
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                ChangeEvent(
                    fileId="file-1",
                    removed=False,
                    file=FileMetadata(
                        id="file-1",
                        name="note.note",
                        parents=(SOURCE_FOLDER_ID,),
                        trashed=True,
                    ),
                ),
            ),
            new_start_page_token="10",
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — treated as a soft delete
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[1] is delete_output
        assert args[2] == "file-1"


class TestWhenChangeIsOutsideSourceFolder:
    def test_files_outside_source_tree_are_skipped(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a .note file whose parent chain does not include SOURCE_FOLDER_ID
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_change("stray", name="stray.note", parents=("OTHER",)),),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: (
            FileMetadata(id="stray", name="stray.note", parents=("OTHER",))
            if fid == "stray"
            else FileMetadata(id="OTHER", name="OtherFolder", parents=())
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN
        enqueue.assert_not_called()


class TestWhenDispatchRaisesMidPage:
    def test_logs_and_skips_the_change_and_still_processes_later_ones(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — three notes; get_metadata for the middle one raises
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="a.note"),
                _change("file-boom", name="b.note"),
                _change("file-3", name="c.note"),
            ),
            new_start_page_token="42",
        )

        def flaky_metadata(fid: str, fields: str | None = None) -> FileMetadata:  # noqa: ARG001
            if fid == "file-boom":
                # Simulate what a since-deleted file's 404 looks like after
                # `_call` wrapping: it surfaces as DrivePermanentError.
                raise DrivePermanentError("HTTP 404 on file-boom")
            return FileMetadata(id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,))

        drive.get_metadata.side_effect = flaky_metadata

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — file-1 and file-3 were enqueued; file-boom was logged-skipped
        enqueued_ids = [c.args[2] for c in enqueue.call_args_list]
        assert enqueued_ids == ["file-1", "file-3"]

        # AND — cursor still advances so we don't re-walk the successful changes
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "42"


class TestWhenDispatchRaisesATransientErrorMidPage:
    def test_transient_error_propagates_and_cursor_stays_at_pre_page_token(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a mid-page transient failure that survived Drive-client retries.
        # We want the workflow to fail so DBOS retries from the last-saved cursor,
        # NOT to swallow the change (the audit's stated concern).
        drive.get_start_page_token.return_value = "SPT-1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="a.note"),
                _change("file-boom", name="b.note"),
            ),
            new_start_page_token="AFTER",
        )

        def flaky_metadata(fid: str, fields: str | None = None) -> FileMetadata:  # noqa: ARG001
            if fid == "file-boom":
                raise DriveTransientError("HTTP 503 after 3 retries")
            return FileMetadata(id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,))

        drive.get_metadata.side_effect = flaky_metadata

        # WHEN / THEN — the transient error propagates
        with (
            patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow"),
            pytest.raises(DriveTransientError, match="503"),
        ):
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # AND — the cursor was seeded to "SPT-1" at start but never advanced
        # past this failing page, so a DBOS retry will replay the same page.
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "SPT-1"


class TestCursorAdvancesPerPageAcrossMultiplePages:
    def test_saves_cursor_after_each_page_not_only_at_the_end(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — two pages; the changes list returns them in order
        drive.get_start_page_token.return_value = "1"

        page_a = ChangesPage(
            changes=(_change("file-a", name="a.note"),),
            next_page_token="TOKEN-B",
        )
        page_b = ChangesPage(
            changes=(_change("file-b", name="b.note"),),
            new_start_page_token="TOKEN-END",
        )
        drive.changes_list.side_effect = [page_a, page_b]
        drive.get_metadata.side_effect = lambda fid, fields=None: FileMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )

        cursor_tokens_after_dispatch: list[str] = []

        def capture_cursor(*args: object, **kwargs: object) -> None:  # noqa: ARG001
            # Whenever a workflow is enqueued, snapshot the current cursor
            with Session(engine) as session:
                saved = cursor.get(session)
            cursor_tokens_after_dispatch.append(saved.page_token if saved else "<none>")

        # WHEN
        with patch(
            "sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow",
            side_effect=capture_cursor,
        ):
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — after enqueuing file-a the cursor still points at the
        # start-of-poll token "1" (page 1 not done yet); after enqueuing
        # file-b the cursor advanced to "TOKEN-B" (page 1 done, so a
        # crash here would resume at page 2, not re-walk page 1).
        assert cursor_tokens_after_dispatch[0] == "1"
        assert cursor_tokens_after_dispatch[1] == "TOKEN-B"

        # AND — final cursor is the last page's new_start_page_token
        with Session(engine) as session:
            final = cursor.get(session)
        assert final is not None
        assert final.page_token == "TOKEN-END"


class TestMetadataCacheAcrossChanges:
    def test_repeated_ancestor_lookups_hit_drive_only_once_per_id(
        self,
        engine: Engine,
        settings: Settings,
        drive: MagicMock,  # noqa: ARG002
    ) -> None:
        # GIVEN — two sibling files in the same subfolder. Both walks
        # through resolve_source_path need `SUB` and root_folder metadata;
        # without caching that's 4 get_metadata calls; with caching it's 2
        # for the sibling files + 1 for `SUB` = 3.
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-a", name="a.note", parents=("SUB",)),
                _change("file-b", name="b.note", parents=("SUB",)),
            ),
            new_start_page_token="10",
        )
        graph = {
            "file-a": FileMetadata(id="file-a", name="a.note", parents=("SUB",)),
            "file-b": FileMetadata(id="file-b", name="b.note", parents=("SUB",)),
            "SUB": FileMetadata(id="SUB", name="Journal", parents=(SOURCE_FOLDER_ID,)),
        }
        seen: list[str] = []

        def spy_metadata(fid: str, fields: str | None = None) -> FileMetadata:  # noqa: ARG001
            seen.append(fid)
            return graph[fid]

        drive.get_metadata.side_effect = spy_metadata

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow"):
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — SUB was fetched exactly once even though both file-a and
        # file-b walked through it during path resolution.
        assert seen.count("SUB") == 1
        assert seen.count("file-a") == 1
        assert seen.count("file-b") == 1


class TestWhenChangesListRejectsTheCursor:
    def test_resets_cursor_and_enqueues_backfill(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — a stored cursor that Drive rejects as 404-permanent
        # (i.e., older than Drive's change-log window). `changes_list`
        # raises `DrivePermanentError`; `get_start_page_token` returns
        # a fresh token that we should reset to.
        with Session(engine) as session, session.begin():
            cursor.set_cursor(session, "STALE-42", datetime(2024, 1, 1, tzinfo=UTC))

        drive.get_start_page_token.return_value = "FRESH-99"
        drive.changes_list.side_effect = DrivePermanentError("HTTP 404: pageToken not found")

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — cursor was reset to the fresh token
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "FRESH-99"

        # AND — a backfill was enqueued to recover missed work
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == "poll_queue"
        assert args[1] is backfill


class TestCursorSeeding:
    def test_first_run_seeds_from_start_page_token(self, engine: Engine, drive: MagicMock) -> None:
        # GIVEN — no cursor yet
        drive.get_start_page_token.return_value = "SEED-1"

        # WHEN
        seed_cursor(drive)

        # THEN
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "SEED-1"

    def test_subsequent_runs_are_noop(self, engine: Engine, drive: MagicMock) -> None:
        # GIVEN — an existing cursor
        with Session(engine) as session, session.begin():
            cursor.set_cursor(session, "EXISTING", datetime(2026, 7, 4, tzinfo=UTC))
        drive.get_start_page_token.return_value = "SHOULD-NOT-BE-USED"

        # WHEN
        seed_cursor(drive)

        # THEN
        drive.get_start_page_token.assert_not_called()
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "EXISTING"
