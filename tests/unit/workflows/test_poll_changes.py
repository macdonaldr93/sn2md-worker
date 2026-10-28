from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from dbos._context import DBOSContext, _dbos_context_var, get_local_dbos_context
from dbos._error import DBOSQueueDeduplicatedError
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import DriveConfig, Settings
from sn2md_worker.db import set_engine
from sn2md_worker.drive.client import (
    DriveClient,
    DrivePermanentError,
    DriveTransientError,
)
from sn2md_worker.drive.models import ChangeEvent, ChangesPage
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.state import conversions, cursor
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.workflows.backfill import backfill
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output
from sn2md_worker.workflows.poll_changes import (
    CONVERT_QUEUE_NAME,
    DELETE_QUEUE_NAME,
    POLL_QUEUE_NAME,
    POLL_TRIGGER_FALLBACK,
    poll_changes,
    poll_changes_impl,
    scheduled_poll_changes,
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
        file=NoteMetadata(id=file_id, name=name, parents=parents),
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
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
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
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
            id=fid, name="notes.note", parents=(SOURCE_FOLDER_ID,)
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — only the .note file is enqueued, and non-.note did not hit get_metadata
        assert enqueue.call_count == 1
        assert enqueue.call_args.args[2] == "file-2"
        drive.get_metadata.assert_called_once_with("file-2", fields="id,name,parents")

    def test_enqueues_delete_output_when_tracked_note_is_renamed_to_non_note_extension(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — file-1 was previously converted as a .note file, then renamed to .bak
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key="Notebooks/renamed.note",
                    current_file_id="file-1",
                    parent_folder_id=SOURCE_FOLDER_ID,
                    source_name="renamed.note",
                    source_path="Notebooks/renamed.note",
                    source_md5="abc",
                    output_rel_path="Notebooks/renamed",
                    last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
                    status=ConversionStatus.SUCCESS,
                ),
            )
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_change("file-1", name="renamed.bak", parents=(SOURCE_FOLDER_ID,)),),
            new_start_page_token="10",
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — delete_output is enqueued without walking parents in Drive
        drive.get_metadata.assert_not_called()
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == DELETE_QUEUE_NAME
        assert args[1] is delete_output
        assert args[2] == "file-1"


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
                    file=NoteMetadata(
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
            NoteMetadata(id="stray", name="stray.note", parents=("OTHER",))
            if fid == "stray"
            else NoteMetadata(id="OTHER", name="OtherFolder", parents=())
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN
        enqueue.assert_not_called()

    def test_enqueues_delete_output_when_previously_converted_note_moves_outside_source_tree(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — file-1 was previously converted inside SOURCE_FOLDER_ID,
        # then moved to OTHER (outside SOURCE_FOLDER_ID) without being trashed.
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key="Notebooks/moved.note",
                    current_file_id="file-1",
                    parent_folder_id=SOURCE_FOLDER_ID,
                    source_name="moved.note",
                    source_path="Notebooks/moved.note",
                    source_md5="abc",
                    output_rel_path="Notebooks/moved",
                    last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
                    status=ConversionStatus.SUCCESS,
                ),
            )
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_change("file-1", name="moved.note", parents=("OTHER",)),),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: (
            NoteMetadata(id="file-1", name="moved.note", parents=("OTHER",))
            if fid == "file-1"
            else NoteMetadata(id="OTHER", name="OtherFolder", parents=())
        )

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — delete_output is enqueued to clean up the orphaned vault directory
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == DELETE_QUEUE_NAME
        assert args[1] is delete_output
        assert args[2] == "file-1"


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

        def flaky_metadata(fid: str, fields: str | None = None) -> NoteMetadata:  # noqa: ARG001
            if fid == "file-boom":
                # Simulate what a since-deleted file's 404 looks like after
                # `_call` wrapping: it surfaces as DrivePermanentError.
                raise DrivePermanentError("HTTP 404 on file-boom")
            return NoteMetadata(id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,))

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

        def flaky_metadata(fid: str, fields: str | None = None) -> NoteMetadata:  # noqa: ARG001
            if fid == "file-boom":
                raise DriveTransientError("HTTP 503 after 3 retries")
            return NoteMetadata(id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,))

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
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
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
            "file-a": NoteMetadata(id="file-a", name="a.note", parents=("SUB",)),
            "file-b": NoteMetadata(id="file-b", name="b.note", parents=("SUB",)),
            "SUB": NoteMetadata(id="SUB", name="Journal", parents=(SOURCE_FOLDER_ID,)),
        }
        seen: list[str] = []

        def spy_metadata(fid: str, fields: str | None = None) -> NoteMetadata:  # noqa: ARG001
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


class TestWhenTheFallbackScheduleFires:
    def test_enqueues_a_fallback_poll_onto_the_poll_queue(self) -> None:
        # GIVEN — the DBOS-scheduled fallback fires. We call `__wrapped__`
        # (the undecorated body) because the `@DBOS.workflow()` wrapper
        # guards against invocation before DBOS is launched, which never
        # happens under unit tests.
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            # WHEN
            scheduled_poll_changes.__wrapped__(
                scheduled_time=datetime(2026, 7, 12, 6, 0, tzinfo=UTC),
                context="cron",
            )

        # THEN — a single poll is enqueued (not run inline) so it
        # serializes behind any webhook-triggered poll on the cursor
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[:3] == (POLL_QUEUE_NAME, poll_changes, POLL_TRIGGER_FALLBACK)
        assert POLL_TRIGGER_FALLBACK == "fallback"

        # AND - each cron tick is a root trigger, so a fresh non-empty
        # correlation id rides along as the trailing workflow arg
        assert isinstance(args[3], str)
        assert args[3]


class TestWhenPollRunsWithAnInheritedCorrelationId:
    def test_convert_and_delete_enqueues_carry_the_same_id(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN - one note update and one removal in the changes feed
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="a.note"),
                _removal("file-gone"),
            ),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )

        # WHEN - the impl runs with an explicit correlation id
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(
                trigger_source="test",
                drive=drive,
                settings=settings,
                correlation_id="corr-abc",
            )

        # THEN - every child enqueue carries the inherited id as its
        # trailing workflow arg
        assert enqueue.call_count == 2
        by_target = {call.args[1]: call.args for call in enqueue.call_args_list}
        assert by_target[convert_note][-1] == "corr-abc"
        assert by_target[delete_output][-1] == "corr-abc"

    def test_cursor_reset_backfill_carries_the_same_id(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN - Drive rejects the cursor, forcing the reset-and-backfill path
        drive.get_start_page_token.return_value = "FRESH-99"
        drive.changes_list.side_effect = DrivePermanentError("HTTP 404: pageToken not found")

        # WHEN
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(
                trigger_source="test",
                drive=drive,
                settings=settings,
                correlation_id="corr-abc",
            )

        # THEN - the recovery backfill inherits the poll's correlation id
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == POLL_QUEUE_NAME
        assert args[1] is backfill
        assert args[2] == "corr-abc"


class TestWhenPollRunsWithoutACorrelationId:
    def test_children_still_get_a_non_empty_minted_id(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN - a pre-upgrade replay: no correlation id was persisted
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(_change("file-1", name="a.note"),),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )

        # WHEN - correlation_id is omitted entirely
        with patch("sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow") as enqueue:
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN - the impl self-heals by minting one and passing it down
        enqueue.assert_called_once()
        minted = enqueue.call_args.args[-1]
        assert isinstance(minted, str)
        assert minted


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


class TestConvertNoteEnqueueDeduplication:
    def test_sets_deduplication_id_and_ignores_duplicate_enqueue_error(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — two note changes; the first succeeds and the second is
        # already queued in DBOS (raises DBOSQueueDeduplicatedError).
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="a.note"),
                _change("file-2", name="b.note"),
            ),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )
        observed_dedup_ids: list[str | None] = []

        def fake_enqueue(*args: object, **_kwargs: object) -> None:
            ctx = get_local_dbos_context()
            observed_dedup_ids.append(ctx.deduplication_id if ctx is not None else None)
            if args[2] == "file-2":
                raise DBOSQueueDeduplicatedError("wf-id", CONVERT_QUEUE_NAME, "file-2:file-2.note")

        # WHEN
        with patch(
            "sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow",
            side_effect=fake_enqueue,
        ):
            poll_changes_impl(trigger_source="test", drive=drive, settings=settings)

        # THEN — deduplication_id was set to "{file_id}:{source_path}" and
        # the duplicate error did not abort the poll or prevent cursor advancement
        assert observed_dedup_ids == ["file-1:file-1.note", "file-2:file-2.note"]
        with Session(engine) as session:
            saved = cursor.get(session)
        assert saved is not None
        assert saved.page_token == "10"

    def test_executes_io_in_dbos_steps_and_enqueues_at_workflow_level(
        self, engine: Engine, settings: Settings, drive: MagicMock
    ) -> None:
        # GIVEN — poll_changes_impl runs inside a DBOS workflow context
        drive.get_start_page_token.return_value = "1"
        drive.changes_list.return_value = ChangesPage(
            changes=(
                _change("file-1", name="a.note"),
                _removal("file-gone"),
            ),
            new_start_page_token="10",
        )
        drive.get_metadata.side_effect = lambda fid, fields=None: NoteMetadata(
            id=fid, name=f"{fid}.note", parents=(SOURCE_FOLDER_ID,)
        )

        parent_ctx = DBOSContext()
        parent_ctx.workflow_id = "parent-poll-wf"
        events: list[tuple[str, str]] = []

        def fake_invoke_step(
            _dbos: object,
            _step_ctx: object,
            func: object,
            args: tuple[object, ...],
            kwargs: dict[str, object],
            *,
            step_name: str,
            **_opts: object,
        ) -> object:
            events.append(("step", step_name))
            return func(*args, **kwargs)  # type: ignore[operator]

        def fake_enqueue(*args: object, **_kwargs: object) -> None:
            ctx = get_local_dbos_context()
            assert ctx is not None and ctx.is_workflow()
            events.append(("enqueue", getattr(args[1], "__name__", str(args[1]))))

        # WHEN
        token = _dbos_context_var.set(parent_ctx)
        try:
            with (
                patch("dbos._dbos._dbos_global_registry.dbos", MagicMock()),
                patch("dbos._core.invoke_step", side_effect=fake_invoke_step),
                patch(
                    "sn2md_worker.workflows.poll_changes.DBOS.enqueue_workflow",
                    side_effect=fake_enqueue,
                ),
            ):
                poll_changes_impl(trigger_source="test", drive=drive, settings=settings)
        finally:
            _dbos_context_var.reset(token)

        # THEN — cursor load, page fetch/resolution, and cursor save run as
        # @DBOS.step() operations, while child workflow enqueues happen at
        # the workflow level between the steps
        assert events == [
            ("step", "_load_or_init_cursor"),
            ("step", "_fetch_page_plan"),
            ("enqueue", "convert_note"),
            ("enqueue", "delete_output"),
            ("step", "_save_cursor"),
        ]


class TestCursorSeedingRaceSafety:
    def test_seed_cursor_does_not_overwrite_cursor_written_during_drive_call(
        self, engine: Engine, drive: MagicMock
    ) -> None:
        # GIVEN — cursor starts empty, and while get_start_page_token() is in
        # flight, another workflow writes a newer cursor "ADVANCED-77".
        def race_token() -> str:
            with Session(engine) as sess, sess.begin():
                cursor.set_cursor(sess, "ADVANCED-77", datetime(2026, 7, 4, tzinfo=UTC))
            return "STALE-SEED-1"

        drive.get_start_page_token.side_effect = race_token

        # WHEN
        seed_cursor(drive)

        # THEN — the advanced cursor is preserved, not clobbered by STALE-SEED-1
        with Session(engine) as sess:
            saved = cursor.get(sess)
        assert saved is not None
        assert saved.page_token == "ADVANCED-77"
