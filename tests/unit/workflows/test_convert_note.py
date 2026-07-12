from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from sn2md_worker.config import Settings, Sn2mdConfig, VaultConfig
from sn2md_worker.conversion.multi_page import MultiPageResult, PageOutcome
from sn2md_worker.db import set_engine
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.sources.protocol import NoteSource
from sn2md_worker.state import conversions, page_conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import Base, ConversionStatus
from sn2md_worker.state.page_conversions import PageConversionUpsert
from sn2md_worker.workflows.convert_note import convert_note_impl

# `import sn2md_worker.workflows.convert_note as convert_note_module` would
# resolve to the DBOS-decorated function (re-exported by workflows/__init__),
# not the module. Use importlib to reach the module for spy hooks.
convert_note_module = importlib.import_module("sn2md_worker.workflows.convert_note")


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
def source() -> MagicMock:
    return MagicMock(spec=NoteSource)


def _file_metadata(**overrides: object) -> NoteMetadata:
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
    return NoteMetadata.model_validate(base)


def _seed_conversion(
    engine: Engine, *, file_id: str, md5: str, status: str = ConversionStatus.SUCCESS
) -> None:
    with Session(engine) as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key="Notebooks/2026-07.note",
                current_file_id=file_id,
                parent_folder_id="p1",
                source_name="2026-07.note",
                source_path="Notebooks/2026-07.note",
                source_md5=md5,
                output_rel_path="Notebooks/2026-07",
                last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
                status=status,
            ),
        )


def _seed_page(engine: Engine, *, page_index: int, page_md5: str) -> None:
    with Session(engine) as session, session.begin():
        page_conversions.upsert(
            session,
            PageConversionUpsert(
                logical_key="Notebooks/2026-07.note",
                page_index=page_index,
                page_md5=page_md5,
                output_rel_path=f"page-{page_index + 1:02d}.md",
                last_converted_at=datetime(2026, 7, 4, tzinfo=UTC),
            ),
        )


def _stub_download(source: MagicMock) -> None:
    def fake(file_id: str, dest_dir: Path, name: str) -> Path:
        target = dest_dir / name
        target.write_bytes(b"note-data")
        return target

    source.download.side_effect = fake


def _fake_result(*, pages: int, cached: dict[int, bool] | None = None) -> MultiPageResult:
    cached = cached or {}
    outcomes = [
        PageOutcome(
            page_index=i,
            page_md5=f"md5-p{i}",
            output_rel_path=f"page-{i + 1:02d}.md",
            was_cached=cached.get(i, False),
        )
        for i in range(pages)
    ]
    return MultiPageResult(pages=outcomes)


def _run_stub(*, pages: int, cached: dict[int, bool] | None = None) -> object:
    """side_effect that fires on_page_done per page like the real runner."""
    result = _fake_result(pages=pages, cached=cached)

    def _side_effect(**kwargs: object) -> MultiPageResult:
        cb = kwargs.get("on_page_done")
        if callable(cb):
            for page in result.pages:
                cb(page)
        return result

    return _side_effect


class TestWhenConvertingANoteForTheFirstTime:
    def test_runs_multi_page_and_persists_a_success_record_and_page_rows(
        self, engine: Engine, settings: Settings, source: MagicMock, tmp_path: Path
    ) -> None:
        # GIVEN
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=3)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/Journal/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — run_multi_page invoked with resolved output dir and secrets
        fake_run.assert_called_once()
        kwargs = fake_run.call_args.kwargs
        assert kwargs["model"] == "fake-model"
        assert kwargs["api_key"] == "fake-key"
        assert kwargs["output_dir"] == tmp_path / "vault" / "Notebooks" / "Journal" / "2026-07"
        assert kwargs["existing_pages"] == {}

        # AND — conversion_record + one page_conversion row per page
        with Session(engine) as session:
            record = conversions.get_by_logical_key(session, "Notebooks/Journal/2026-07.note")
            pages = page_conversions.list_for_note(session, "Notebooks/Journal/2026-07.note")
        assert record is not None
        assert record.last_status == ConversionStatus.SUCCESS
        assert len(pages) == 3
        assert [p.page_index for p in pages] == [0, 1, 2]


class TestWhenAllPagesAreCached:
    def test_still_records_success_without_regenerating_state(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — a previous conversion with 2 pages
        _seed_conversion(engine, file_id="file-old", md5="old-md5")
        _seed_page(engine, page_index=0, page_md5="md5-p0")
        _seed_page(engine, page_index=1, page_md5="md5-p1")
        source.get_metadata.return_value = _file_metadata(id="file-new", md5Checksum="new-md5")
        _stub_download(source)

        # WHEN — the note re-converts with all pages reporting cache hits
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=2, cached={0: True, 1: True})
            convert_note_impl(
                file_id="file-new",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — existing pages are passed in and the record is refreshed
        assert fake_run.call_args.kwargs["existing_pages"] == {0: "md5-p0", 1: "md5-p1"}
        with Session(engine) as session:
            pages = page_conversions.list_for_note(session, "Notebooks/2026-07.note")
        assert len(pages) == 2


class TestWhenTheNoteHasFewerPagesThanBefore:
    def test_prunes_page_rows_beyond_the_new_page_count(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — a stale 3-page state
        for i in range(3):
            _seed_page(engine, page_index=i, page_md5=f"md5-old-{i}")
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN — the current note has only 2 pages
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=2)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — page 3 row is dropped
        with Session(engine) as session:
            pages = page_conversions.list_for_note(session, "Notebooks/2026-07.note")
        assert [p.page_index for p in pages] == [0, 1]

    def test_removes_stale_page_files_from_disk(
        self, engine: Engine, settings: Settings, source: MagicMock, tmp_path: Path
    ) -> None:
        # GIVEN — a stale page file left over from a prior 3-page conversion
        note_dir = tmp_path / "vault" / "Notebooks" / "2026-07"
        note_dir.mkdir(parents=True)
        (note_dir / "page-03.md").write_text("stale")
        (note_dir / "page-03.png").write_bytes(b"stale-png")
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=2)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN
        assert not (note_dir / "page-03.md").exists()
        assert not (note_dir / "page-03.png").exists()


class TestWhenAPngOnlyOrphanExists:
    def test_removes_the_orphan_png_beyond_current_page_count(
        self,
        engine: Engine,
        settings: Settings,
        source: MagicMock,
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        # GIVEN — page-04.png with no peer page-04.md (a crash between
        # `copy2(png)` and `write_text(md)` in a prior run)
        note_dir = tmp_path / "vault" / "Notebooks" / "2026-07"
        note_dir.mkdir(parents=True)
        (note_dir / "page-04.png").write_bytes(b"orphan-png")
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN — the new conversion produces 2 pages
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=2)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — the orphan PNG is gone even though no .md ever mentioned it
        assert not (note_dir / "page-04.png").exists()


class TestWhenAnOldFlatMarkdownFileExists:
    def test_cleans_up_the_legacy_flat_md_and_sidecar(
        self, engine: Engine, settings: Settings, source: MagicMock, tmp_path: Path
    ) -> None:
        # GIVEN — a pre-multi-page single-file conversion
        note_dir = tmp_path / "vault" / "Notebooks" / "2026-07"
        note_dir.mkdir(parents=True)
        (note_dir / "2026-07.md").write_text("legacy flat")
        (note_dir / ".sn2md.metadata.yaml").write_text("v1")
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=1)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN
        assert not (note_dir / "2026-07.md").exists()
        assert not (note_dir / ".sn2md.metadata.yaml").exists()


class TestWhenTheRecordIsAlreadyUpToDate:
    def test_neither_downloads_nor_reruns(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN
        _seed_conversion(engine, file_id="file-1", md5="abc123")
        source.get_metadata.return_value = _file_metadata()

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN
        fake_run.assert_not_called()
        source.download.assert_not_called()


class TestWhenTheFileIsTrashed:
    def test_neither_downloads_nor_runs(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN
        source.get_metadata.return_value = _file_metadata(trashed=True)

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN
        fake_run.assert_not_called()
        source.download.assert_not_called()


class TestWhenNoGeminiKeyIsConfigured:
    def test_raises_a_configuration_error(
        self,
        engine: Engine,
        source: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # GIVEN — neither settings.sn2md.api_key nor LLM_GEMINI_KEY set
        monkeypatch.delenv("LLM_GEMINI_KEY", raising=False)
        settings = Settings(
            vault=VaultConfig(root_path=tmp_path / "vault", mirror_source_layout=True),
            sn2md=Sn2mdConfig(model="fake", api_key=None),
        )
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN / THEN
        with (
            patch("sn2md_worker.workflows.convert_note.run_multi_page"),
            pytest.raises(RuntimeError, match="no Gemini API key configured"),
        ):
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

    def test_falls_back_to_llm_gemini_key_env_var(
        self,
        engine: Engine,
        source: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # GIVEN
        monkeypatch.setenv("LLM_GEMINI_KEY", "from-env")
        settings = Settings(
            vault=VaultConfig(root_path=tmp_path / "vault", mirror_source_layout=True),
            sn2md=Sn2mdConfig(model="fake-model", api_key=None),
        )
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN
        with patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run:
            fake_run.side_effect = _run_stub(pages=1)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN
        fake_run.assert_called_once()
        assert fake_run.call_args.kwargs["api_key"] == "from-env"


class TestWhenTheWorkflowCrashesMidNote:
    def test_pages_persisted_before_the_crash_remain_in_the_db(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN — callback fires for pages 0, 1, then run_multi_page raises.
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        def crash_after_two_pages(**kwargs: object) -> MultiPageResult:
            cb = kwargs.get("on_page_done")
            assert callable(cb)
            for i in range(2):
                cb(
                    PageOutcome(
                        page_index=i,
                        page_md5=f"md5-p{i}",
                        output_rel_path=f"page-{i + 1:02d}.md",
                        was_cached=False,
                    )
                )
            raise RuntimeError("gemini blew up on page 3")

        # WHEN
        with (
            patch(
                "sn2md_worker.workflows.convert_note.run_multi_page",
                side_effect=crash_after_two_pages,
            ),
            pytest.raises(RuntimeError, match="page 3"),
        ):
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — pages 0, 1 survive for the retry to hash-cache.
        with Session(engine) as session:
            pages = page_conversions.list_for_note(session, "Notebooks/2026-07.note")
            record = conversions.get_by_logical_key(session, "Notebooks/2026-07.note")
        assert [p.page_index for p in pages] == [0, 1]
        # AND — record stays PENDING so `_already_up_to_date` won't short-circuit.
        assert record is not None
        assert record.last_status == ConversionStatus.PENDING


class TestPendingRecordWrittenBeforeMultiPageRuns:
    def test_parent_record_exists_as_pending_when_run_multi_page_starts(
        self, engine: Engine, settings: Settings, source: MagicMock
    ) -> None:
        # GIVEN
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)
        observed_status: dict[str, str] = {}

        def check_record(**kwargs: object) -> MultiPageResult:  # noqa: ARG001
            with Session(engine) as session:
                record = conversions.get_by_logical_key(session, "Notebooks/2026-07.note")
            assert record is not None
            observed_status["at_start"] = record.last_status
            return _fake_result(pages=1)

        # WHEN
        with patch(
            "sn2md_worker.workflows.convert_note.run_multi_page",
            side_effect=check_record,
        ):
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — PENDING mid-run, SUCCESS after finalize.
        assert observed_status["at_start"] == ConversionStatus.PENDING
        with Session(engine) as session:
            record = conversions.get_by_logical_key(session, "Notebooks/2026-07.note")
        assert record is not None
        assert record.last_status == ConversionStatus.SUCCESS


class TestConvertNoteAcquiresLockAroundWork:
    def test_lock_for_is_invoked_with_the_logical_key(
        self,
        source: MagicMock,
        settings: Settings,
        engine: Engine,  # noqa: ARG002
        tmp_path: Path,  # noqa: ARG002
    ) -> None:
        # GIVEN
        source.get_metadata.return_value = _file_metadata()
        _stub_download(source)

        # WHEN — spy on the lock helper as imported into convert_note
        with (
            patch(
                "sn2md_worker.workflows.convert_note.lock_for",
                wraps=convert_note_module.lock_for,
            ) as spy,
            patch("sn2md_worker.workflows.convert_note.run_multi_page") as fake_run,
        ):
            fake_run.side_effect = _run_stub(pages=1)
            convert_note_impl(
                file_id="file-1",
                source_path="Notebooks/2026-07.note",
                source=source,
                settings=settings,
            )

        # THEN — invoked exactly once with the normalized logical_key
        spy.assert_called_once_with("Notebooks/2026-07.note")
