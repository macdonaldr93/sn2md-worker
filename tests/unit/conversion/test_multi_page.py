from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from sn2md_worker.conversion.multi_page import (
    index_filename,
    page_filename,
    page_index_from_filename,
    run_multi_page,
)

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


class TestPageFilename:
    def test_zero_index_becomes_page_01(self) -> None:
        assert page_filename(0) == "page-01.md"

    def test_ninth_index_becomes_page_10(self) -> None:
        assert page_filename(9) == "page-10.md"


class TestPageIndexFromFilename:
    def test_round_trips_with_page_filename(self) -> None:
        for idx in (0, 1, 9, 99):
            assert page_index_from_filename(page_filename(idx)) == idx

    def test_returns_none_for_non_page_names(self) -> None:
        assert page_index_from_filename("index.md") is None
        assert page_index_from_filename("random.md") is None
        assert page_index_from_filename("page-.md") is None
        assert page_index_from_filename("page-XX.md") is None


class TestIndexFilename:
    def test_is_index_md(self) -> None:
        assert index_filename() == "index.md"


def _stub_extract(pngs: list[Path]):
    class FakeExtractor:
        def extract_images(self, filename: str, dest: str) -> list[str]:  # noqa: ARG002
            return [str(p) for p in pngs]

    return FakeExtractor


class TestRunMultiPageFreshConversion:
    def test_writes_one_page_md_per_page_plus_index_and_calls_gemini_for_each(
        self, tmp_path: Path
    ) -> None:
        # GIVEN — two page PNGs staged on disk with distinct content
        pngs_src = tmp_path / "src"
        pngs_src.mkdir()
        page1 = pngs_src / "p1.png"
        page2 = pngs_src / "p2.png"
        page1.write_bytes(b"png-1-content")
        page2.write_bytes(b"png-2-content")

        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        pages_returned: list[Path] = []

        def fake_extract_images(self, filename: str, dest: str) -> list[str]:  # noqa: ARG001
            # The runner uses a temp dir; copy the sources in so they can be hashed.
            dest_dir = Path(dest)
            copied = []
            for src in (page1, page2):
                target = dest_dir / src.name
                target.write_bytes(src.read_bytes())
                copied.append(target)
            pages_returned.extend(copied)
            return [str(p) for p in copied]

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                fake_extract_images,
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["md for page 1", "md for page 2"],
            ) as fake_llm,
        ):
            result = run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
            )

        # THEN — both pages ran through Gemini
        assert fake_llm.call_count == 2
        assert result.gemini_calls == 2
        assert result.cache_hits == 0

        # AND — page files and the index are on disk
        assert (output_dir / "page-01.md").is_file()
        assert (output_dir / "page-01.png").is_file()
        assert (output_dir / "page-02.md").is_file()
        assert (output_dir / "page-02.png").is_file()
        assert (output_dir / "index.md").is_file()

        # AND — the index links to each page as an Obsidian wikilink
        index_body = (output_dir / "index.md").read_text()
        assert "[[page-01]]" in index_body
        assert "[[page-02]]" in index_body


class TestRunMultiPageWithCachedPages:
    def test_skips_gemini_for_pages_whose_hash_matches(self, tmp_path: Path) -> None:
        # GIVEN — one page whose hash we've already seen, one new
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        output_dir.mkdir(parents=True)

        # Pre-populate page-01.md so the cache hit can read it as context.
        (output_dir / "page-01.md").write_text("previous content for page 1")

        def fake_extract_images(self, filename: str, dest: str) -> list[str]:  # noqa: ARG001
            dest_dir = Path(dest)
            (dest_dir / "p1.png").write_bytes(b"page-1-bytes")
            (dest_dir / "p2.png").write_bytes(b"page-2-bytes")
            return [str(dest_dir / "p1.png"), str(dest_dir / "p2.png")]

        # md5(b"page-1-bytes") — precompute for the cache key
        import hashlib

        page1_md5 = hashlib.md5(b"page-1-bytes", usedforsecurity=False).hexdigest()

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                fake_extract_images,
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["md for page 2"],
            ) as fake_llm,
        ):
            result = run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={0: page1_md5},
                now=NOW,
            )

        # THEN — only page 2 hit Gemini
        assert fake_llm.call_count == 1
        assert result.cache_hits == 1
        assert result.gemini_calls == 1
        assert result.pages[0].was_cached is True
        assert result.pages[1].was_cached is False
