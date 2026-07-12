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
from sn2md_worker.sources.models import ListedNote, NoteMetadata
from sn2md_worker.sources.protocol import NoteSource
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.workflows.backfill import backfill, backfill_impl, scheduled_backfill
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.poll_changes import CONVERT_QUEUE_NAME, POLL_QUEUE_NAME

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
SOURCE_FOLDER_ID = "SRC"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'backfill.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(drive=DriveConfig(source_folder_id=SOURCE_FOLDER_ID))


@pytest.fixture
def source() -> MagicMock:
    return MagicMock(spec=NoteSource)


def _listed(
    file_id: str, source_path: str, *, name: str = "note.note", md5: str = "abc"
) -> ListedNote:
    metadata = NoteMetadata(
        id=file_id,
        name=name,
        md5Checksum=md5,
        parents=(SOURCE_FOLDER_ID,),
    )
    return ListedNote(metadata=metadata, source_path=source_path)


def _seed_success(
    engine: Engine,
    *,
    logical_key: str,
    file_id: str,
    md5: str,
    status: str = ConversionStatus.SUCCESS,
) -> None:
    with Session(engine) as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key=logical_key,
                current_file_id=file_id,
                parent_folder_id="p1",
                source_name="note.note",
                source_path=logical_key,
                source_md5=md5,
                output_rel_path=logical_key.rsplit(".note", 1)[0],
                last_converted_at=NOW,
                status=status,
            ),
        )


class TestWhenNothingIsConvertedYet:
    def test_enqueues_every_note_found_in_drive(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — three notes in Drive at various depths
        source.list_all_notes.return_value = iter(
            [
                _listed("f1", "Notebooks/2026-07.note", name="2026-07.note"),
                _listed("f2", "Notebooks/2026-08.note", name="2026-08.note"),
                _listed("f3", "stray.note", name="stray.note"),
            ]
        )

        # WHEN
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings)

        # THEN — every note is enqueued
        assert enqueue.call_count == 3
        enqueued_ids = {call.args[2] for call in enqueue.call_args_list}
        assert enqueued_ids == {"f1", "f2", "f3"}
        # AND — all onto the convert queue with the convert_note fn
        first_call = enqueue.call_args_list[0]
        assert first_call.args[0] == CONVERT_QUEUE_NAME
        assert first_call.args[1] is convert_note


class TestWhenNotesAreAlreadyConverted:
    def test_skips_notes_whose_md5_matches_and_status_is_success(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — one note already up-to-date, one with different md5, one missing
        _seed_success(
            engine,
            logical_key="Notebooks/kept.note",
            file_id="f-kept",
            md5="same-md5",
        )
        _seed_success(
            engine,
            logical_key="Notebooks/stale.note",
            file_id="f-stale",
            md5="OLD-md5",
        )
        source.list_all_notes.return_value = iter(
            [
                _listed("f-kept", "Notebooks/kept.note", md5="same-md5"),
                _listed("f-stale-new", "Notebooks/stale.note", md5="NEW-md5"),
                _listed("f-fresh", "Notebooks/fresh.note", md5="fresh-md5"),
            ]
        )

        # WHEN
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings)

        # THEN — only the changed and missing notes are enqueued
        assert enqueue.call_count == 2
        enqueued_ids = {call.args[2] for call in enqueue.call_args_list}
        assert enqueued_ids == {"f-stale-new", "f-fresh"}


class TestWhenPreviousRunEndedInError:
    def test_reenqueues_notes_with_error_status(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — a record with matching md5 but ERROR status
        _seed_success(
            engine,
            logical_key="Notebooks/failed.note",
            file_id="f-failed",
            md5="abc",
            status=ConversionStatus.ERROR,
        )
        source.list_all_notes.return_value = iter(
            [_listed("f-failed", "Notebooks/failed.note", md5="abc")]
        )

        # WHEN
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings)

        # THEN — still enqueued despite the md5 match
        enqueue.assert_called_once()


class TestConversionRecordsAreBulkLoadedOnce:
    def test_single_list_all_by_key_call_regardless_of_note_count(
        self,
        engine: Engine,
        settings: Settings,
        source: MagicMock,  # noqa: ARG002
    ) -> None:
        # GIVEN — three seeded records + three Drive files
        for i in range(3):
            _seed_success(
                engine,
                logical_key=f"Notebooks/seeded-{i}.note",
                file_id=f"seed-{i}",
                md5=f"md5-{i}",
            )
        source.list_all_notes.return_value = iter(
            [_listed(f"f{i}", f"Notebooks/f{i}.note", md5="new-md5") for i in range(3)]
        )

        # WHEN — spy on list_all_by_key while running
        with (
            patch(
                "sn2md_worker.workflows.backfill.conversions.list_all_by_key",
                wraps=conversions.list_all_by_key,
            ) as spy,
            patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow"),
        ):
            backfill_impl(source=source, settings=settings)

        # THEN — one bulk load, not N per-file lookups
        assert spy.call_count == 1


class TestWhenBackfillRunsWithAnInheritedCorrelationId:
    def test_convert_enqueues_carry_the_same_id(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN - two unconverted notes
        source.list_all_notes.return_value = iter(
            [
                _listed("f1", "Notebooks/2026-07.note", name="2026-07.note"),
                _listed("f2", "Notebooks/2026-08.note", name="2026-08.note"),
            ]
        )

        # WHEN - the impl runs with an explicit correlation id
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings, correlation_id="corr-abc")

        # THEN - every convert_note enqueue inherits the id as its
        # trailing workflow arg
        assert enqueue.call_count == 2
        assert all(call.args[-1] == "corr-abc" for call in enqueue.call_args_list)


class TestWhenBackfillRunsWithoutACorrelationId:
    def test_convert_enqueues_still_get_a_non_empty_minted_id(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN - a pre-upgrade replay: no correlation id was persisted
        source.list_all_notes.return_value = iter([_listed("f1", "Notebooks/2026-07.note")])

        # WHEN - correlation_id is omitted entirely
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings)

        # THEN - the impl self-heals by minting one and passing it down
        enqueue.assert_called_once()
        minted = enqueue.call_args.args[-1]
        assert isinstance(minted, str)
        assert minted


class TestWhenSourceFolderIsNotConfigured:
    def test_skips_gracefully(self, engine: Engine, source: MagicMock) -> None:
        # GIVEN — settings with an empty source_folder_id
        settings = Settings(drive=DriveConfig(source_folder_id=""))

        # WHEN
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            backfill_impl(source=source, settings=settings)

        # THEN — no Drive call, no enqueue
        source.list_all_notes.assert_not_called()
        enqueue.assert_not_called()


class TestWhenTheBackfillSweepScheduleFires:
    def test_enqueues_a_backfill_onto_the_poll_queue(self) -> None:
        # GIVEN - the DBOS-scheduled sweep fires. We call `__wrapped__`
        # (the undecorated body) because the `@DBOS.workflow()` wrapper
        # guards against invocation before DBOS is launched, which never
        # happens under unit tests.
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            # WHEN
            scheduled_backfill.__wrapped__(
                scheduled_time=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
                context="cron",
            )

        # THEN - a single backfill is enqueued (not run inline) so it
        # serializes on poll_queue behind any in-flight poll or backfill
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[:2] == (POLL_QUEUE_NAME, backfill)

        # AND - each cron tick is a root trigger, so a fresh non-empty
        # correlation id rides along as the trailing workflow arg
        assert isinstance(args[2], str)
        assert args[2]

    def test_two_ticks_mint_different_correlation_ids(self) -> None:
        # GIVEN - two consecutive cron ticks
        with patch("sn2md_worker.workflows.backfill.DBOS.enqueue_workflow") as enqueue:
            # WHEN
            scheduled_backfill.__wrapped__(
                scheduled_time=datetime(2026, 7, 12, 5, 0, tzinfo=UTC),
                context="cron",
            )
            scheduled_backfill.__wrapped__(
                scheduled_time=datetime(2026, 7, 13, 5, 0, tzinfo=UTC),
                context="cron",
            )

        # THEN - each tick is its own root trigger carrying its own id
        assert enqueue.call_count == 2
        ids = [call.args[2] for call in enqueue.call_args_list]
        assert all(ids)
        assert ids[0] != ids[1]
