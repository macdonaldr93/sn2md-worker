from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from tenacity import wait_none

from sn2md_worker.conversion import multi_page
from sn2md_worker.conversion.multi_page import (
    DEFAULT_PROMPT,
    Sn2mdRunError,
    index_filename,
    page_filename,
    page_index_from_filename,
    run_multi_page,
)


@pytest.fixture
def instant_gemini_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-out tenacity's backoff sleeps so retry tests run fast."""
    monkeypatch.setattr(multi_page._call_gemini_with_retry.retry, "wait", wait_none())


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


class TestSortByPageNumber:
    def test_10_sorts_after_2_not_between_1_and_2(self, tmp_path: Path) -> None:
        # GIVEN — sn2md-style filenames where lexical sort would misorder
        paths = [
            tmp_path / "page-1.png",
            tmp_path / "page-10.png",
            tmp_path / "page-2.png",
            tmp_path / "page-3.png",
        ]

        # WHEN
        ordered = multi_page._sort_by_page_number(paths)

        # THEN
        assert [p.name for p in ordered] == [
            "page-1.png",
            "page-2.png",
            "page-3.png",
            "page-10.png",
        ]

    def test_files_without_digits_land_at_the_end(self, tmp_path: Path) -> None:
        # GIVEN
        paths = [
            tmp_path / "cover.png",
            tmp_path / "page-2.png",
            tmp_path / "page-1.png",
        ]

        # WHEN
        ordered = multi_page._sort_by_page_number(paths)

        # THEN — numeric-named pages first, then the odd one out
        assert [p.name for p in ordered] == ["page-1.png", "page-2.png", "cover.png"]


class TestDefaultPrompt:
    def test_contains_context_placeholder(self) -> None:
        assert "{context}" in DEFAULT_PROMPT

    def test_requires_ascii_only_mermaid(self) -> None:
        # Cheap regression guard: if someone edits the prompt and drops
        # the ASCII rule, this fires.
        assert "ASCII" in DEFAULT_PROMPT
        assert "mermaid" in DEFAULT_PROMPT.lower()


def _fake_extract_factory(pngs_map: dict[str, bytes]) -> Any:
    """Return a callable that, when patched onto NotebookExtractor.extract_images,
    writes the given (name → bytes) pngs into the destination dir."""

    def fake_extract_images(self, filename: str, dest: str) -> list[str]:  # noqa: ARG001
        dest_dir = Path(dest)
        paths = []
        for name, content in pngs_map.items():
            path = dest_dir / name
            path.write_bytes(content)
            paths.append(str(path))
        return paths

    return fake_extract_images


class TestRunMultiPageOrdersPagesByNumericFilenamePart:
    def test_extract_returning_dict_ordered_1_10_2_still_writes_in_numeric_order(
        self, tmp_path: Path
    ) -> None:
        # GIVEN — sn2md returns filenames whose lexical sort would misorder
        # them: page-10 would land between page-1 and page-2. Our sort
        # extracts the numeric part first so page-10 lands last.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory(
                    {
                        "page-1.png": b"content-1",
                        "page-10.png": b"content-10",
                        "page-2.png": b"content-2",
                    }
                ),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["MD-1", "MD-2", "MD-10"],
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

        # THEN — Gemini received the sources in numeric order (1 → 2 → 10)
        source_order = [Path(call.args[0]).name for call in fake_llm.call_args_list]
        assert source_order == ["page-1.png", "page-2.png", "page-10.png"]

        # AND — the vault gets consecutive page-01/02/03 files (not with a gap
        # where "page-10" would land in the middle under a lexical sort).
        assert result.pages[0].output_rel_path == "page-01.md"
        assert result.pages[1].output_rel_path == "page-02.md"
        assert result.pages[2].output_rel_path == "page-03.md"


class TestRunMultiPageFreshConversion:
    def test_writes_one_page_md_per_page_plus_index_and_calls_gemini_for_each(
        self, tmp_path: Path
    ) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"png-1-content", "p2.png": b"png-2-content"}),
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

        # THEN
        assert fake_llm.call_count == 2
        assert result.gemini_calls == 2
        assert result.cache_hits == 0

        assert (output_dir / "page-01.md").is_file()
        assert (output_dir / "page-01.png").is_file()
        assert (output_dir / "page-02.md").is_file()
        assert (output_dir / "page-02.png").is_file()
        assert (output_dir / "index.md").is_file()

        index_body = (output_dir / "index.md").read_text()
        assert "[[page-01]]" in index_body
        assert "[[page-02]]" in index_body


class TestRunMultiPageWithCachedPages:
    def test_skips_gemini_for_pages_whose_hash_matches(self, tmp_path: Path) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        output_dir.mkdir(parents=True)
        (output_dir / "page-01.md").write_text("previous content for page 1")

        page1_md5 = hashlib.md5(b"page-1-bytes", usedforsecurity=False).hexdigest()

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes", "p2.png": b"page-2-bytes"}),
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

        assert fake_llm.call_count == 1
        assert result.cache_hits == 1
        assert result.gemini_calls == 1
        assert result.pages[0].was_cached is True
        assert result.pages[1].was_cached is False


class TestRunMultiPageWhenCacheHashMatchesButPageMdIsMissing:
    def test_reruns_gemini_because_the_output_file_is_gone(self, tmp_path: Path) -> None:
        # GIVEN — the state DB knows a page md5, but the on-disk page-01.md
        # has been deleted (e.g., someone dragged it to Obsidian's trash).
        # The cache check ANDs `existing_pages == md5` with `page_md.exists()`,
        # so a stale hit without the file must re-run Gemini.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        output_dir.mkdir(parents=True)
        # No page-01.md written to disk
        page1_md5 = hashlib.md5(b"page-1-bytes", usedforsecurity=False).hexdigest()

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                return_value="regenerated markdown",
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

        # THEN — Gemini was called even though the hash "matched"
        assert fake_llm.call_count == 1
        assert result.gemini_calls == 1
        assert result.cache_hits == 0
        assert result.pages[0].was_cached is False
        assert (output_dir / "page-01.md").exists()


class TestRunMultiPageWhenExtractReturnsZeroPages:
    def test_writes_an_index_and_no_page_files(self, tmp_path: Path) -> None:
        # GIVEN — the .note file exists but sn2md's extract returns nothing
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
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

        # THEN — no Gemini calls, no page files, just an index recording zero pages
        fake_llm.assert_not_called()
        assert result.pages == []
        assert list(output_dir.glob("page-*.md")) == []
        assert list(output_dir.glob("page-*.png")) == []
        index_body = (output_dir / "index.md").read_text()
        assert "0 pages" in index_body


class TestRunMultiPageWhenGeminiFailsMidWay:
    def test_leaves_earlier_pages_on_disk_and_raises_for_the_failing_one(
        self,
        tmp_path: Path,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN — three pages, Gemini fails permanently on page 2 (after tenacity retries)
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        def flaky(*args: object, **kwargs: object) -> str:
            # Second call fails; the retry decorator burns 3 attempts on it.
            source = str(args[0])
            if "p2.png" in source:
                raise RuntimeError("gemini down for page 2")
            return f"md for {Path(source).name}"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory(
                    {
                        "p1.png": b"page-1-bytes",
                        "p2.png": b"page-2-bytes",
                        "p3.png": b"page-3-bytes",
                    }
                ),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=flaky,
            ),
            pytest.raises(Sn2mdRunError, match="page 2"),
        ):
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
            )

        # THEN — page 1 already wrote to disk before the failure; no cleanup.
        # A subsequent retry will overwrite page-01.md idempotently, and the
        # workflow-level `_cleanup_stale_pages` prunes anything beyond the
        # eventual page count. Documented behavior.
        assert (output_dir / "page-01.md").exists()
        # Page 2 raised before its .md write, so nothing was written for it.
        assert not (output_dir / "page-02.md").exists()
        # Page 3 was never reached.
        assert not (output_dir / "page-03.md").exists()


class TestRunMultiPageHonorsCustomPrompt:
    def test_calls_gemini_with_the_override_prompt(self, tmp_path: Path) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        custom_prompt = "MY OVERRIDE — {context}"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                return_value="md",
            ) as fake_llm,
        ):
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
                prompt=custom_prompt,
            )

        # THEN — the custom prompt was passed through as the last positional arg
        # to image_to_markdown(path, context, api_key, model, prompt).
        assert fake_llm.call_args.args[4] == custom_prompt


class TestRunMultiPageWhenGeminiFailsTransientlyThenSucceeds:
    def test_retries_and_writes_the_markdown_from_the_successful_attempt(
        self,
        tmp_path: Path,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        attempt_counter = {"n": 0}

        def flaky(*_args: object, **_kwargs: object) -> str:
            attempt_counter["n"] += 1
            if attempt_counter["n"] < 3:
                raise ConnectionError("gemini transient blip")
            return "recovered markdown"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=flaky,
            ) as fake_llm,
        ):
            # WHEN
            result = run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
            )

        # THEN
        assert fake_llm.call_count == 3
        assert result.gemini_calls == 1  # one page, ultimately transcribed
        assert "recovered markdown" in (output_dir / "page-01.md").read_text()


class TestRunMultiPageWhenGeminiKeepsFailing:
    def test_gives_up_after_max_attempts_and_preserves_type_info(
        self,
        tmp_path: Path,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=ConnectionError("gemini down"),
            ) as fake_llm,
            pytest.raises(Sn2mdRunError, match="ConnectionError"),
        ):
            # WHEN
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
            )

        # THEN — three attempts (initial + 2 retries), then wrapped
        assert fake_llm.call_count == 3


class TestFallsBackToDefaultPromptWhenNoneSupplied:
    def test_calls_gemini_with_default_prompt(self, tmp_path: Path) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                return_value="md",
            ) as fake_llm,
        ):
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
                # prompt omitted → DEFAULT_PROMPT
            )

        assert fake_llm.call_args.args[4] == DEFAULT_PROMPT
