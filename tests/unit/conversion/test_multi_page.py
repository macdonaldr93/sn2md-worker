from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sn2md_worker.conversion.multi_page import (
    DEFAULT_PROMPT,
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

    def test_falls_back_to_default_prompt_when_none_supplied(self, tmp_path: Path) -> None:
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
