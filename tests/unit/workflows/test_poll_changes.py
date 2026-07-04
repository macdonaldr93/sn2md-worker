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
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.drive.models import ChangeEvent, ChangesPage, FileMetadata
from sn2md_worker.state import cursor
from sn2md_worker.state.models import Base
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.poll_changes import CONVERT_QUEUE_NAME, poll_changes_impl, seed_cursor

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
    def test_removals_are_logged_and_skipped(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_removal("file-1"),),
            new_start_page_token="10",
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — nothing enqueued (deletion mirroring lands in a later slice)
        enqueue.assert_not_called()

    def test_trashed_files_are_skipped(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN
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

        # THEN
        enqueue.assert_not_called()


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
