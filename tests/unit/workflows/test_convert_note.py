from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import Settings, Sn2mdConfig, VaultConfig
from sn2md_worker.db import set_engine
from sn2md_worker.drive.client import DriveClient
from sn2md_worker.drive.models import FileMetadata
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.workflows.convert_note import convert_note_impl


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = create_engine(f"sqlite:///{tmp_path / 'test.sqlite'}", future=True)
    Base.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        vault=VaultConfig(root_path=tmp_path / "vault", mirror_source_layout=True),
        sn2md=Sn2mdConfig(model="fake-model", api_key=SecretStr("fake-key")),
    )


@pytest.fixture
def drive() -> MagicMock:
    return MagicMock(spec=DriveClient)


def _file_metadata(**overrides: object) -> FileMetadata:
    base: dict[str, object] = {
        "id": "file-1",
        "name": "2026-07.note",
        "md5Checksum": "abc123",
        "size": 100,
        "parents": ("p1",),
        "mimeType": "application/octet-stream",
        "trashed": False,
    }
    base.update(overrides)
    return FileMetadata.model_validate(base)


def test_converts_and_persists_new_note(
    engine: Engine, settings: Settings, drive: MagicMock, tmp_path: Path
) -> None:
    drive.get_metadata.return_value = _file_metadata()

    def fake_download(file_id: str, dest_dir: Path, name: str) -> Path:
        target = dest_dir / name
        target.write_bytes(b"note-data")
        return target

    drive.download.side_effect = fake_download

    with patch("sn2md_worker.workflows.convert_note.run_sn2md") as fake_run:
        convert_note_impl(
            file_id="file-1",
            source_path="Notebooks/Journal/2026-07.note",
            drive=drive,
            settings=settings,
        )

    fake_run.assert_called_once()
    call_kwargs = fake_run.call_args.kwargs
    assert call_kwargs["model"] == "fake-model"
    assert call_kwargs["api_key"] == "fake-key"
    assert call_kwargs["output_dir"] == tmp_path / "vault" / "Notebooks" / "Journal"

    with Session(engine) as session:
        record = conversions.get_by_logical_key(session, "Notebooks/Journal/2026-07.note")

    assert record is not None
    assert record.current_file_id == "file-1"
    assert record.parent_folder_id == "p1"
    assert record.source_md5 == "abc123"
    assert record.output_rel_path == "Notebooks/Journal/2026-07"
    assert record.last_status == ConversionStatus.SUCCESS


def test_skips_when_already_up_to_date(
    engine: Engine, settings: Settings, drive: MagicMock
) -> None:
    with Session(engine) as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key="Notebooks/2026-07.note",
                current_file_id="file-1",
                parent_folder_id="p1",
                source_name="2026-07.note",
                source_path="Notebooks/2026-07.note",
                source_md5="abc123",
                output_rel_path="Notebooks/2026-07",
                last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
                status=ConversionStatus.SUCCESS,
            ),
        )

    drive.get_metadata.return_value = _file_metadata()

    with patch("sn2md_worker.workflows.convert_note.run_sn2md") as fake_run:
        convert_note_impl(
            file_id="file-1",
            source_path="Notebooks/2026-07.note",
            drive=drive,
            settings=settings,
        )

    fake_run.assert_not_called()
    drive.download.assert_not_called()


def test_reconverts_when_md5_changed(engine: Engine, settings: Settings, drive: MagicMock) -> None:
    with Session(engine) as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key="Notebooks/2026-07.note",
                current_file_id="file-old",
                parent_folder_id="p1",
                source_name="2026-07.note",
                source_path="Notebooks/2026-07.note",
                source_md5="old-md5",
                output_rel_path="Notebooks/2026-07",
                last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
                status=ConversionStatus.SUCCESS,
            ),
        )

    drive.get_metadata.return_value = _file_metadata(id="file-new", md5Checksum="new-md5")

    def fake_download(file_id: str, dest_dir: Path, name: str) -> Path:
        (dest_dir / name).write_bytes(b"x")
        return dest_dir / name

    drive.download.side_effect = fake_download

    with patch("sn2md_worker.workflows.convert_note.run_sn2md") as fake_run:
        convert_note_impl(
            file_id="file-new",
            source_path="Notebooks/2026-07.note",
            drive=drive,
            settings=settings,
        )

    fake_run.assert_called_once()

    with Session(engine) as session:
        record = conversions.get_by_logical_key(session, "Notebooks/2026-07.note")

    assert record is not None
    assert record.current_file_id == "file-new"
    assert record.source_md5 == "new-md5"
    assert record.attempts == 2


def test_skips_trashed_files(engine: Engine, settings: Settings, drive: MagicMock) -> None:
    drive.get_metadata.return_value = _file_metadata(trashed=True)

    with patch("sn2md_worker.workflows.convert_note.run_sn2md") as fake_run:
        convert_note_impl(
            file_id="file-1",
            source_path="Notebooks/2026-07.note",
            drive=drive,
            settings=settings,
        )

    fake_run.assert_not_called()
    drive.download.assert_not_called()


def test_raises_when_no_gemini_key(engine: Engine, drive: MagicMock, tmp_path: Path) -> None:
    settings = Settings(
        vault=VaultConfig(root_path=tmp_path / "vault", mirror_source_layout=True),
        sn2md=Sn2mdConfig(model="fake", api_key=None),
    )
    drive.get_metadata.return_value = _file_metadata()

    def fake_download(file_id: str, dest_dir: Path, name: str) -> Path:
        (dest_dir / name).write_bytes(b"x")
        return dest_dir / name

    drive.download.side_effect = fake_download

    with (
        patch("sn2md_worker.workflows.convert_note.run_sn2md"),
        pytest.raises(RuntimeError, match="no Gemini API key configured"),
    ):
        convert_note_impl(
            file_id="file-1",
            source_path="Notebooks/2026-07.note",
            drive=drive,
            settings=settings,
        )
