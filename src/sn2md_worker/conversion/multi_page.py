"""Per-page conversion runner.

Bypasses `sn2md.import_supernote_file_core` (which produces a single
concatenated Markdown file per note) so we can:

1. Cache per-page LLM output — a page whose rendered PNG hasn't changed
   since the last convert doesn't need to be re-transcribed.
2. Emit one `page-NN.md` per page + an `index.md` linking them, so
   Obsidian shows a folder-per-note.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sn2md.ai_utils import image_to_markdown
from sn2md.importers.note import NotebookExtractor
from sn2md.types import TO_MARKDOWN_TEMPLATE

__all__ = [
    "MultiPageResult",
    "PageOutcome",
    "Sn2mdRunError",
    "index_filename",
    "page_filename",
    "page_index_from_filename",
    "run_multi_page",
]


@dataclass(frozen=True)
class PageOutcome:
    page_index: int  # 0-indexed
    page_md5: str
    output_rel_path: str  # path relative to the note's output_dir, e.g. "page-01.md"
    was_cached: bool


@dataclass(frozen=True)
class MultiPageResult:
    pages: list[PageOutcome]

    @property
    def cache_hits(self) -> int:
        return sum(1 for p in self.pages if p.was_cached)

    @property
    def gemini_calls(self) -> int:
        return sum(1 for p in self.pages if not p.was_cached)


class Sn2mdRunError(RuntimeError):
    """Raised when sn2md fails mid-conversion (extract or LLM call)."""


def run_multi_page(
    *,
    note_path: Path,
    output_dir: Path,
    model: str,
    api_key: str,
    existing_pages: dict[int, str],
    now: datetime,
) -> MultiPageResult:
    """Convert every page of `note_path`, skipping pages whose PNG hash
    matches what's in `existing_pages` at the same index.

    Writes `output_dir/page-NN.md` + `output_dir/page-NN.png` per page and
    an `output_dir/index.md` linking them.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    outcomes: list[PageOutcome] = []

    with tempfile.TemporaryDirectory(prefix="sn2md-pages-") as extract_root:
        pngs = _extract_pngs(note_path, Path(extract_root))
        previous_markdown = ""
        for page_index, png_path in enumerate(pngs):
            page_md5 = _hash_file(png_path)
            page_md = output_dir / page_filename(page_index)
            page_png = output_dir / _asset_filename(page_index)
            cached = existing_pages.get(page_index) == page_md5 and page_md.exists()

            if cached:
                previous_markdown = page_md.read_text(encoding="utf-8")
            else:
                context = _tail(previous_markdown)
                try:
                    llm_output = image_to_markdown(
                        str(png_path), context, api_key, model, TO_MARKDOWN_TEMPLATE
                    )
                except Exception as exc:  # noqa: BLE001
                    raise Sn2mdRunError(
                        f"Gemini failed on page {page_index + 1} of {note_path.name}: {exc}"
                    ) from exc
                shutil.copy2(png_path, page_png)
                rendered = _render_page(
                    page_index=page_index,
                    total=len(pngs),
                    llm_output=llm_output,
                    asset_name=page_png.name,
                    created_on=now.date().isoformat(),
                )
                page_md.write_text(rendered, encoding="utf-8")
                previous_markdown = rendered

            outcomes.append(
                PageOutcome(
                    page_index=page_index,
                    page_md5=page_md5,
                    output_rel_path=page_md.name,
                    was_cached=cached,
                )
            )

    _write_index(output_dir, outcomes, note_basename=note_path.stem, now=now)
    return MultiPageResult(pages=outcomes)


def page_filename(page_index: int) -> str:
    """`page-01.md` for index 0, `page-02.md` for index 1, ..."""
    return f"page-{page_index + 1:02d}.md"


def index_filename() -> str:
    return "index.md"


def page_index_from_filename(name: str) -> int | None:
    """Reverse of `page_filename`: parse `page-NN.md` back to the 0-index.

    Returns `None` when the name doesn't fit the pattern — used by
    cleanup code to walk the note's output directory safely.
    """
    stem = name.removesuffix(".md")
    if not stem.startswith("page-"):
        return None
    try:
        return int(stem.removeprefix("page-")) - 1
    except ValueError:
        return None


def _asset_filename(page_index: int) -> str:
    return f"page-{page_index + 1:02d}.png"


def _extract_pngs(note_path: Path, dest: Path) -> list[Path]:
    try:
        raw = NotebookExtractor().extract_images(str(note_path), str(dest))
    except Exception as exc:  # noqa: BLE001
        raise Sn2mdRunError(f"sn2md failed to extract images from {note_path.name}: {exc}") from exc
    return [Path(p) for p in raw]


def _hash_file(path: Path) -> str:
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def _tail(markdown: str, length: int = 200) -> str:
    """Last N chars of the previous page's markdown, for LLM context."""
    return markdown[-length:] if markdown else ""


def _render_page(
    *,
    page_index: int,
    total: int,
    llm_output: str,
    asset_name: str,
    created_on: str,
) -> str:
    page_number = page_index + 1
    return (
        f"---\n"
        f"created: {created_on}\n"
        f"tags: supernote\n"
        f"page: {page_number}\n"
        f"of: {total}\n"
        f"---\n\n"
        f"{llm_output.strip()}\n\n"
        f"![Page {page_number}]({asset_name})\n"
    )


def _write_index(
    output_dir: Path,
    outcomes: list[PageOutcome],
    *,
    note_basename: str,
    now: datetime,
) -> None:
    lines = [
        "---",
        f"created: {now.date().isoformat()}",
        "tags: supernote",
        "---",
        "",
        f"# {note_basename}",
        "",
        f"{len(outcomes)} page{'s' if len(outcomes) != 1 else ''}, transcribed from Supernote.",
        "",
    ]
    for outcome in outcomes:
        page_stem = outcome.output_rel_path.removesuffix(".md")
        lines.append(f"- [[{page_stem}]]")
    lines.append("")
    (output_dir / index_filename()).write_text("\n".join(lines), encoding="utf-8")
