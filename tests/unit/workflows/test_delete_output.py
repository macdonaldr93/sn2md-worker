from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import Settings, VaultConfig
from sn2md_worker.db import set_engine
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.sources.protocol import NoteSource
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.delete_output import delete_output_impl
from sn2md_worker.workflows.locks import lock_for
from sn2md_worker.workflows.queues import CONVERT_QUEUE_NAME

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
LOGICAL_KEY = "Notebooks/2026-07.note"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'delete.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.fixture
def settings(vault_root: Path) -> Settings:
    return Settings(vault=VaultConfig(root_path=vault_root, mirror_source_layout=True))


@pytest.fixture
def source() -> MagicMock:
    return MagicMock(spec=NoteSource)


def _seed_record(engine: Engine, *, file_id: str, parent_folder_id: str) -> None:
    with Session(engine) as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key=LOGICAL_KEY,
                current_file_id=file_id,
                parent_folder_id=parent_folder_id,
                source_name="2026-07.note",
                source_path=LOGICAL_KEY,
                source_md5="abc",
                output_rel_path="Notebooks/2026-07",
                last_converted_at=NOW,
                status=ConversionStatus.SUCCESS,
            ),
        )


def _seed_output_dir(vault_root: Path) -> Path:
    note_dir = vault_root / "Notebooks" / "2026-07"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "2026-07.md").write_text("# note")
    (note_dir / "img.png").write_bytes(b"png")
    return note_dir


class TestWhenNoRecordExistsForFileId:
    def test_is_a_noop(self, engine: Engine, settings: Settings, source: MagicMock) -> None:
        # WHEN
        delete_output_impl(file_id="unknown", source=source, settings=settings)

        # THEN — no Drive call, no exception
        source.find_live_note.assert_not_called()


class TestWhenLiveFileStillExistsAtSameLocation:
    def test_repoints_current_file_id_without_deleting_vault(
        self, engine: Engine, settings: Settings, source: MagicMock, vault_root: Path
    ) -> None:
        # GIVEN — a stored record for the OLD file id, and Drive still has a live
        # file with the same name at the same parent (Supernote replace pattern)
        _seed_record(engine, file_id="file-old", parent_folder_id="parent-1")
        note_dir = _seed_output_dir(vault_root)
        source.find_live_note.return_value = NoteMetadata(
            id="file-new", name="2026-07.note", parents=("parent-1",)
        )

        # WHEN
        with patch("sn2md_worker.workflows.delete_output.DBOS.enqueue_workflow") as enqueue:
            delete_output_impl(file_id="file-old", source=source, settings=settings)

        # THEN — vault output remains
        assert (note_dir / "2026-07.md").exists()

        # AND — record's current_file_id is repointed
        with Session(engine) as session:
            record = conversions.get_by_logical_key(session, LOGICAL_KEY)
        assert record is not None
        assert record.current_file_id == "file-new"

        # AND — convert_note is explicitly enqueued for the replacement, so the
        # vault won't go stale even if the create-side push was missed.
        enqueue.assert_called_once()
        args, _ = enqueue.call_args
        assert args[0] == CONVERT_QUEUE_NAME
        assert args[1] is convert_note
        assert args[2] == "file-new"
        assert args[3] == LOGICAL_KEY  # source_path == logical_key in this fixture

        # AND - a non-empty correlation id (self-heal minted here, since
        # none was passed in) rides along as the trailing workflow arg
        assert isinstance(args[4], str)
        assert args[4]


class TestWhenRepointRunsWithAnInheritedCorrelationId:
    def test_repoint_enqueue_carries_the_same_id(
        self, engine: Engine, settings: Settings, source: MagicMock, vault_root: Path
    ) -> None:
        # GIVEN - the Supernote replace pattern (stale delete, live replacement)
        _seed_record(engine, file_id="file-old", parent_folder_id="parent-1")
        _seed_output_dir(vault_root)
        source.find_live_note.return_value = NoteMetadata(
            id="file-new", name="2026-07.note", parents=("parent-1",)
        )

        # WHEN - the impl runs with an explicit correlation id
        with patch("sn2md_worker.workflows.delete_output.DBOS.enqueue_workflow") as enqueue:
            delete_output_impl(
                file_id="file-old",
                source=source,
                settings=settings,
                correlation_id="corr-abc",
            )

        # THEN - the repoint convert_note enqueue inherits the id
        enqueue.assert_called_once()
        assert enqueue.call_args.args[-1] == "corr-abc"


class TestWhenNoLiveFileExistsAtSameLocation:
    def test_removes_vault_output_and_record(
        self, engine: Engine, settings: Settings, source: MagicMock, vault_root: Path
    ) -> None:
        # GIVEN — record + on-disk output, Drive returns None
        _seed_record(engine, file_id="file-1", parent_folder_id="parent-1")
        note_dir = _seed_output_dir(vault_root)
        source.find_live_note.return_value = None

        # WHEN
        delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — the per-note directory is gone
        assert not note_dir.exists()

        # AND — the record is gone
        with Session(engine) as session:
            record = conversions.get_by_logical_key(session, LOGICAL_KEY)
        assert record is None


class TestWhenRecordHasNoParentFolderId:
    def test_falls_through_to_delete_without_drive_check(
        self, engine: Engine, settings: Settings, source: MagicMock, vault_root: Path
    ) -> None:
        # GIVEN — legacy record with no parent_folder_id
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key=LOGICAL_KEY,
                    current_file_id="file-1",
                    parent_folder_id=None,
                    source_name="2026-07.note",
                    source_path=LOGICAL_KEY,
                    source_md5="abc",
                    output_rel_path="Notebooks/2026-07",
                    last_converted_at=NOW,
                    status=ConversionStatus.SUCCESS,
                ),
            )
        note_dir = _seed_output_dir(vault_root)

        # WHEN
        delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — no Drive lookup, vault deleted
        source.find_live_note.assert_not_called()
        assert not note_dir.exists()


class TestWhenOutputRelPathIsEmpty:
    def test_refuses_to_delete(
        self, engine: Engine, settings: Settings, source: MagicMock, vault_root: Path
    ) -> None:
        # GIVEN — a corrupt record with empty output_rel_path
        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key=LOGICAL_KEY,
                    current_file_id="file-1",
                    parent_folder_id="parent-1",
                    source_name="2026-07.note",
                    source_path=LOGICAL_KEY,
                    source_md5="abc",
                    output_rel_path="",
                    last_converted_at=NOW,
                    status=ConversionStatus.SUCCESS,
                ),
            )
        source.find_live_note.return_value = None
        # Seed something in the vault to prove we don't touch it
        stray = vault_root / "important"
        stray.mkdir()

        # WHEN
        delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — vault root and its contents are untouched
        assert stray.exists()
        # AND — record still exists (guard prevented delete_by_logical_key)
        with Session(engine) as session:
            assert conversions.get_by_logical_key(session, LOGICAL_KEY) is not None


class TestDeleteOutputAcquiresLockAroundWork:
    def test_lock_for_is_invoked_with_the_logical_key(
        self,
        engine: Engine,  # noqa: ARG002
        settings: Settings,
        source: MagicMock,
        vault_root: Path,
    ) -> None:
        # GIVEN
        _seed_record(engine, file_id="file-1", parent_folder_id="parent-1")
        _seed_output_dir(vault_root)
        source.find_live_note.return_value = None

        # WHEN
        with patch(
            "sn2md_worker.workflows.delete_output.lock_for",
            wraps=lock_for,
        ) as spy:
            delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — locked before rmtree'ing under a possibly-concurrent convert.
        spy.assert_called_once_with(LOGICAL_KEY)


class TestWhenTheRecordIsRepointedByAConcurrentConvert:
    def test_second_read_inside_the_lock_re_checks_current_file_id(
        self,
        engine: Engine,
        settings: Settings,
        source: MagicMock,
        vault_root: Path,
    ) -> None:
        # GIVEN — a convert repoints current_file_id between our two reads.
        _seed_record(engine, file_id="file-1", parent_folder_id="parent-1")
        _seed_output_dir(vault_root)

        original_get = conversions.get_by_current_file_id
        call_count = {"n": 0}

        def racy_get(session, file_id):
            call_count["n"] += 1
            if call_count["n"] == 2:
                with Session(engine) as write_sess, write_sess.begin():
                    conversions.set_current_file_id(
                        write_sess, logical_key=LOGICAL_KEY, new_file_id="file-2"
                    )
            return original_get(session, file_id)

        # WHEN
        with patch(
            "sn2md_worker.workflows.delete_output.conversions.get_by_current_file_id",
            side_effect=racy_get,
        ):
            delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — bails with no_record_after_lock; vault + record intact.
        source.find_live_note.assert_not_called()
        assert (vault_root / "Notebooks" / "2026-07" / "2026-07.md").exists()
        with Session(engine) as session:
            assert conversions.get_by_logical_key(session, LOGICAL_KEY) is not None


class TestWhenOutputRelPathEscapesVault:
    def test_refuses_to_delete_paths_outside_vault(
        self,
        engine: Engine,
        settings: Settings,
        source: MagicMock,
        vault_root: Path,
        tmp_path: Path,
    ) -> None:
        # GIVEN — a malicious/corrupt record that walks up out of the vault
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "canary.txt").write_text("keep me")

        with Session(engine) as session, session.begin():
            conversions.upsert(
                session,
                ConversionUpsert(
                    logical_key=LOGICAL_KEY,
                    current_file_id="file-1",
                    parent_folder_id="parent-1",
                    source_name="2026-07.note",
                    source_path=LOGICAL_KEY,
                    source_md5="abc",
                    output_rel_path="../outside",
                    last_converted_at=NOW,
                    status=ConversionStatus.SUCCESS,
                ),
            )
        source.find_live_note.return_value = None

        # WHEN
        delete_output_impl(file_id="file-1", source=source, settings=settings)

        # THEN — the outside directory is untouched
        assert (outside / "canary.txt").exists()
