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
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sn2md.importers.note import NotebookExtractor
from sn2md.types import TO_MARKDOWN_TEMPLATE

from sn2md_worker.conversion.gemini import transcribe_page

__all__ = [
    "DEFAULT_PROMPT",
    "MultiPageResult",
    "PageOutcome",
    "Sn2mdRunError",
    "index_filename",
    "page_filename",
    "page_index_from_filename",
    "run_multi_page",
]

# We take sn2md's `TO_MARKDOWN_TEMPLATE` as our base — same {context}
# placeholder, same core rules — and layer on stricter guidance so
# Gemini stops producing mermaid blocks with Unicode arrows that break
# the parser. Override at runtime via `[sn2md] prompt = "..."` in
# config.toml if you need something different.
_STRICTER_GUIDANCE = """
- If you produce a ```mermaid``` code block: use ONLY ASCII characters
  inside it. No Unicode arrows (→, ⇒, ⟶), en/em dashes
  (–, —), smart quotes (“”‘’), ellipses
  (…), or emoji. Use ASCII substitutes (-->, ->>, --, ...,
  "straight quotes"). Quote node labels with spaces or punctuation:
  A["My Node"]. Stick to standard diagram types (flowchart,
  sequenceDiagram, classDiagram, stateDiagram, erDiagram, gantt, pie,
  journey). If you can't produce syntactically valid mermaid, output
  an ASCII sketch inside a plain fenced code block instead.
- Escape unbalanced backticks, angle brackets, and pipe characters in
  prose so the surrounding Markdown stays valid CommonMark.
"""

DEFAULT_PROMPT = TO_MARKDOWN_TEMPLATE.rstrip() + "\n" + _STRICTER_GUIDANCE.lstrip()


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
    prompt: str | None = None,
    on_page_done: Callable[[PageOutcome], None] | None = None,
) -> MultiPageResult:
    """Convert every page of `note_path`, reusing prior LLM output by
    hash so a page inserted mid-note doesn't cascade re-transcription
    across every subsequent page. `on_page_done` fires per page after
    the .md write so callers can persist state incrementally.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    prompt_template = prompt or DEFAULT_PROMPT
    outcomes: list[PageOutcome] = []
    cached_bodies_by_hash = _preload_cached_bodies(existing_pages, output_dir)

    with tempfile.TemporaryDirectory(prefix="sn2md-pages-") as extract_root:
        pngs = _extract_pngs(note_path, Path(extract_root))
        previous_markdown = ""
        for page_index, png_path in enumerate(pngs):
            page_bytes = png_path.read_bytes()
            page_md5 = hashlib.md5(page_bytes, usedforsecurity=False).hexdigest()
            page_md = output_dir / page_filename(page_index)
            page_png = output_dir / _asset_filename(page_index)
            cached_body = cached_bodies_by_hash.get(page_md5)

            if cached_body is not None:
                body = cached_body
            else:
                context = _tail(previous_markdown)
                try:
                    body = transcribe_page(
                        png_path=png_path,
                        context=context,
                        api_key=api_key,
                        model=model,
                        prompt_template=prompt_template,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Preserve type name so structured log downstream can
                    # tell 429/5xx (retried, still failed) from 400 auth /
                    # payload errors (would have failed on attempt 1).
                    raise Sn2mdRunError(
                        f"Gemini failed on page {page_index + 1} of "
                        f"{note_path.name} after retries "
                        f"({type(exc).__name__}: {exc})"
                    ) from exc

            rendered = _render_page(
                page_index=page_index,
                total=len(pngs),
                llm_output=body,
                asset_name=page_png.name,
                created_on=now.date().isoformat(),
            )
            # Unconditional write — cache-hit at a shifted index would
            # otherwise leave nothing at the new slot.
            page_png.write_bytes(page_bytes)
            page_md.write_text(rendered, encoding="utf-8")
            previous_markdown = rendered

            outcome = PageOutcome(
                page_index=page_index,
                page_md5=page_md5,
                output_rel_path=page_md.name,
                was_cached=cached_body is not None,
            )
            outcomes.append(outcome)
            if on_page_done is not None:
                on_page_done(outcome)

    _write_index(output_dir, outcomes, note_basename=note_path.stem, now=now)
    return MultiPageResult(pages=outcomes)


def page_filename(page_index: int) -> str:
    """`page-01.md` for index 0, `page-02.md` for index 1, ..."""
    return f"page-{page_index + 1:02d}.md"


def index_filename() -> str:
    return "index.md"


def page_index_from_filename(name: str) -> int | None:
    """Parse `page-NN.md` or `page-NN.png` back to the 0-index.

    Returns `None` when the name doesn't fit either pattern — used by
    cleanup code to walk the note's output directory safely. The `.png`
    variant is what lets `_cleanup_stale_pages` prune PNG-only orphans
    left behind if we crashed between `copy2(png)` and `write_text(md)`.
    """
    stem = name.removesuffix(".md").removesuffix(".png")
    if not stem.startswith("page-"):
        return None
    try:
        return int(stem.removeprefix("page-")) - 1
    except ValueError:
        return None


# Anchored to \Z so we only strip the render's own trailing image line,
# not an inline image the user might have added mid-body.
_TRAILING_IMAGE_RE = re.compile(r"\n\n!\[Page \d+\]\(page-\d+\.png\)\n?\Z")
_FRONTMATTER_CLOSE = "\n---\n\n"


def _extract_cached_body(rendered: str) -> str | None:
    """Peel frontmatter + trailing image ref off a prior `page-NN.md`.

    Returns None for unparseable files (user edit removed the image ref,
    legacy pre-multi-page format) so those pages fall back to Gemini.
    """
    close_idx = rendered.find(_FRONTMATTER_CLOSE)
    if close_idx == -1:
        return None
    body_with_image = rendered[close_idx + len(_FRONTMATTER_CLOSE) :]
    stripped, replacements = _TRAILING_IMAGE_RE.subn("", body_with_image)
    if replacements == 0:
        return None
    return stripped


def _preload_cached_bodies(existing_pages: dict[int, str], output_dir: Path) -> dict[str, str]:
    """Index prior page bodies by md5 for hash-first cache lookup. Two
    passes: DB rows use their recorded md5; orphan .md files (crashed
    between .md write and DB upsert) key on the sibling .png's md5."""
    bodies: dict[str, str] = {}
    covered: set[int] = set()

    for old_index, page_md5 in existing_pages.items():
        body = _read_body_at(output_dir, old_index)
        if body is None:
            continue
        bodies.setdefault(page_md5, body)
        covered.add(old_index)

    if not output_dir.is_dir():
        return bodies

    for md_path in output_dir.glob("page-*.md"):
        idx = page_index_from_filename(md_path.name)
        if idx is None or idx in covered:
            continue
        png_path = output_dir / _asset_filename(idx)
        if not png_path.is_file():
            continue
        try:
            png_bytes = png_path.read_bytes()
        except OSError:
            continue
        png_md5 = hashlib.md5(png_bytes, usedforsecurity=False).hexdigest()
        body = _read_body_at(output_dir, idx)
        if body is None:
            continue
        bodies.setdefault(png_md5, body)

    return bodies


def _read_body_at(output_dir: Path, page_index: int) -> str | None:
    path = output_dir / page_filename(page_index)
    if not path.is_file():
        return None
    try:
        rendered = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_cached_body(rendered)


def _asset_filename(page_index: int) -> str:
    return f"page-{page_index + 1:02d}.png"


def _extract_pngs(note_path: Path, dest: Path) -> list[Path]:
    try:
        raw = NotebookExtractor().extract_images(str(note_path), str(dest))
    except Exception as exc:  # noqa: BLE001
        raise Sn2mdRunError(f"sn2md failed to extract images from {note_path.name}: {exc}") from exc
    return _sort_by_page_number([Path(p) for p in raw])


_PAGE_NUMBER_RE = re.compile(r"\d+")


def _sort_by_page_number(paths: list[Path]) -> list[Path]:
    """Sort by the first integer in the filename, then by name for ties.

    Guards against sn2md's undocumented filename ordering: lexical sort
    puts `page-10.png` before `page-2.png`, which would misalign the
    (index, hash) cache key and rewrite pages into the wrong slots.
    """

    def key(path: Path) -> tuple[int, str]:
        match = _PAGE_NUMBER_RE.search(path.stem)
        return (int(match.group(0)) if match else 10**9, path.name)

    return sorted(paths, key=key)


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
