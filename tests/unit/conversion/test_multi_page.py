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
    PageOutcome,
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


def _write_rendered_page(
    *, output_dir: Path, page_index: int, total: int, body: str, created_on: str
) -> None:
    """Write a page-NN.md file in the exact shape `_render_page` produces
    so `_preload_cached_bodies` can round-trip it as a cache hit."""
    page_num = page_index + 1
    output_dir.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\n"
        f"created: {created_on}\n"
        f"tags: supernote\n"
        f"page: {page_num}\n"
        f"of: {total}\n"
        f"---\n\n"
        f"{body}\n\n"
        f"![Page {page_num}](page-{page_num:02d}.png)\n"
    )
    (output_dir / f"page-{page_num:02d}.md").write_text(text, encoding="utf-8")


def _md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


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
        # GIVEN — a prior page-01.md rendered in the expected shape
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        _write_rendered_page(
            output_dir=output_dir,
            page_index=0,
            total=1,
            body="previous content for page 1",
            created_on="2026-07-04",
        )

        page1_md5 = _md5_hex(b"page-1-bytes")

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
        # AND — the cached body is preserved, but frontmatter reflects the new total
        cached_md = (output_dir / "page-01.md").read_text()
        assert "previous content for page 1" in cached_md
        assert "of: 2" in cached_md


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


class TestRunMultiPageWhenAPageIsInsertedMidNote:
    def test_only_the_new_page_calls_gemini_and_shifted_pages_reuse_bodies(
        self, tmp_path: Path
    ) -> None:
        # GIVEN — prior 3-page conversion on disk + DB
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"

        old_pngs = [b"png-A", b"png-B", b"png-C"]
        for i, body in enumerate(["body A", "body B", "body C"]):
            _write_rendered_page(
                output_dir=output_dir,
                page_index=i,
                total=3,
                body=body,
                created_on="2026-07-04",
            )
        existing_pages = {i: _md5_hex(png) for i, png in enumerate(old_pngs)}

        # WHEN — NEW page inserted at index 1; A, B, C shift to 0, 2, 3.
        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory(
                    {
                        "p1.png": b"png-A",
                        "p2.png": b"png-NEW",
                        "p3.png": b"png-B",
                        "p4.png": b"png-C",
                    }
                ),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["body NEW"],
            ) as fake_llm,
        ):
            result = run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages=existing_pages,
                now=NOW,
            )

        # THEN — one Gemini call for the inserted page; shifted pages cache-hit.
        assert fake_llm.call_count == 1
        assert result.gemini_calls == 1
        assert result.cache_hits == 3
        assert [p.was_cached for p in result.pages] == [True, False, True, True]

        # AND — body B (was page-02.md) is now page-03.md with fresh frontmatter.
        page03 = (output_dir / "page-03.md").read_text()
        assert "body B" in page03
        assert "of: 4" in page03
        assert "page: 3" in page03
        assert "![Page 3](page-03.png)" in page03

        page02 = (output_dir / "page-02.md").read_text()
        assert "body NEW" in page02


class TestRunMultiPageInvokesOnPageDone:
    def test_callback_fires_once_per_page_with_the_written_outcome(self, tmp_path: Path) -> None:
        # GIVEN
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        received: list[PageOutcome] = []

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes", "p2.png": b"page-2-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["md 1", "md 2"],
            ),
        ):
            # WHEN
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
                on_page_done=received.append,
            )

        # THEN — one callback per page, fired after each .md was written.
        assert [p.page_index for p in received] == [0, 1]
        assert [p.output_rel_path for p in received] == ["page-01.md", "page-02.md"]

    def test_callback_not_invoked_for_pages_that_fail_gemini(
        self,
        tmp_path: Path,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN — page 2 fails, page 1 succeeds.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        received: list[PageOutcome] = []

        def flaky(*args: object, **_kwargs: object) -> str:
            source = str(args[0])
            if "p2.png" in source:
                raise RuntimeError("gemini down")
            return "md 1"

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"png-1", "p2.png": b"png-2"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=flaky,
            ),
            pytest.raises(Sn2mdRunError),
        ):
            # WHEN
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
                on_page_done=received.append,
            )

        # THEN — only page 1's callback fired.
        assert [p.page_index for p in received] == [0]


class TestRunMultiPageWhenSeveralPriorPagesShareTheSameHash:
    def test_all_shifted_blank_pages_cache_hit_from_the_same_body(self, tmp_path: Path) -> None:
        # GIVEN — three blank pages, same md5. Collapses to one entry via
        # setdefault; safe because identical content → identical body.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        for i in range(3):
            _write_rendered_page(
                output_dir=output_dir,
                page_index=i,
                total=3,
                body="(blank page)",
                created_on="2026-07-04",
            )
        blank_md5 = _md5_hex(b"blank-png-bytes")
        existing_pages = {0: blank_md5, 1: blank_md5, 2: blank_md5}

        # Now three blank pages plus a new one at the end.
        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory(
                    {
                        "p1.png": b"blank-png-bytes",
                        "p2.png": b"blank-png-bytes",
                        "p3.png": b"blank-png-bytes",
                        "p4.png": b"new-page-bytes",
                    }
                ),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                side_effect=["fresh content"],
            ) as fake_llm,
        ):
            result = run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages=existing_pages,
                now=NOW,
            )

        # THEN — three cache-hits from one shared blank-page body, one
        # Gemini call for the new final page. The reused body appears at
        # all three blank positions with correct per-page frontmatter.
        assert fake_llm.call_count == 1
        assert result.cache_hits == 3
        assert result.gemini_calls == 1
        for i in range(3):
            page_md = (output_dir / page_filename(i)).read_text()
            assert "(blank page)" in page_md
            assert f"page: {i + 1}" in page_md
            assert "of: 4" in page_md


class TestRunMultiPageRecoversAnOrphanMdWhenDbLostItsRow:
    def test_orphan_md_with_sibling_png_still_cache_hits(self, tmp_path: Path) -> None:
        # GIVEN — prior run wrote .md + .png but crashed before the DB
        # upsert, so existing_pages doesn't know about this page.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        _write_rendered_page(
            output_dir=output_dir,
            page_index=0,
            total=1,
            body="orphan body from crashed run",
            created_on="2026-07-04",
        )
        (output_dir / "page-01.png").write_bytes(b"page-1-bytes")

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
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

        # THEN — no Gemini call; the orphan body was recovered
        fake_llm.assert_not_called()
        assert result.gemini_calls == 0
        assert result.cache_hits == 1
        assert result.pages[0].was_cached is True
        assert "orphan body from crashed run" in (output_dir / "page-01.md").read_text()


class TestRunMultiPageDoesNotRecoverOrphansWithoutASiblingPng:
    def test_falls_back_to_gemini_when_only_the_md_exists(self, tmp_path: Path) -> None:
        # GIVEN — orphan page-01.md but no sibling .png to hash.
        note_path = tmp_path / "note.note"
        note_path.write_bytes(b"note-bytes")
        output_dir = tmp_path / "vault" / "note"
        _write_rendered_page(
            output_dir=output_dir,
            page_index=0,
            total=1,
            body="orphan body without png",
            created_on="2026-07-04",
        )
        # No sibling PNG — the orphan pass silently skips this.

        with (
            patch(
                "sn2md_worker.conversion.multi_page.NotebookExtractor.extract_images",
                _fake_extract_factory({"p1.png": b"page-1-bytes"}),
            ),
            patch(
                "sn2md_worker.conversion.multi_page.image_to_markdown",
                return_value="fresh gemini output",
            ) as fake_llm,
        ):
            run_multi_page(
                note_path=note_path,
                output_dir=output_dir,
                model="fake-model",
                api_key="fake-key",
                existing_pages={},
                now=NOW,
            )

        assert fake_llm.call_count == 1
        assert "fresh gemini output" in (output_dir / "page-01.md").read_text()


class TestExtractCachedBodyRejectsMalformedFiles:
    def test_missing_frontmatter_returns_none(self) -> None:
        assert multi_page._extract_cached_body("just some markdown\n") is None

    def test_missing_trailing_image_returns_none(self) -> None:
        rendered = (
            "---\ncreated: 2026-07-04\ntags: supernote\npage: 1\nof: 2\n---\n\n"
            "body content without trailing image\n"
        )
        assert multi_page._extract_cached_body(rendered) is None

    def test_wellformed_page_round_trips(self) -> None:
        rendered = (
            "---\ncreated: 2026-07-04\ntags: supernote\npage: 3\nof: 5\n---\n\n"
            "the body\n\n"
            "![Page 3](page-03.png)\n"
        )
        assert multi_page._extract_cached_body(rendered) == "the body"


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
