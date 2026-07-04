# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "sn2md>=2.7.0",
#   "llm-gemini>=0.32",
# ]
# ///
"""
Verify that sn2md's Python API converts a .note file to Markdown using
Gemini 2.5 Pro via the llm-gemini plugin.

See scripts/verify/README.md for setup.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


def die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    if len(sys.argv) < 2:
        die("usage: 02_sn2md_gemini.py <path/to/file.note>")

    note_path = Path(sys.argv[1]).expanduser().resolve()
    if not note_path.exists():
        die(f"file not found: {note_path}")
    if note_path.suffix.lower() != ".note":
        die(f"not a .note file: {note_path}")

    api_key = os.environ.get("LLM_GEMINI_KEY")
    if not api_key:
        die("LLM_GEMINI_KEY not set")

    model = os.environ.get("SN2MD_MODEL", "gemini/gemini-2.5-pro")

    # Imports deferred so a missing env var reports before an import
    # traceback confuses the user.
    from sn2md.importer import import_supernote_file_core
    from sn2md.importers.note import NotebookExtractor
    from sn2md.types import Config

    print(f"input:   {note_path}")
    print(f"model:   {model}")

    with tempfile.TemporaryDirectory(prefix="sn2md-verify-") as tmp:
        cfg = Config(model=model, api_key=api_key)
        print(f"output:  {tmp}\n")

        started = time.monotonic()
        try:
            import_supernote_file_core(
                image_extractor=NotebookExtractor(),
                file_name=str(note_path),
                output=tmp,
                config=cfg,
                force=True,
                progress=True,
                model=None,
            )
        except Exception as e:
            print(f"\nsn2md raised: {type(e).__name__}: {e}", file=sys.stderr)
            print(
                "if this is a model-name error, try SN2MD_MODEL=gemini/gemini-2.5-pro",
                file=sys.stderr,
            )
            return 1
        elapsed = time.monotonic() - started

        tmp_root = Path(tmp)
        print(f"\nsn2md finished in {elapsed:.1f}s")
        print("output tree:")
        for p in sorted(tmp_root.rglob("*")):
            if p.is_file():
                rel = p.relative_to(tmp_root)
                print(f"  {rel}  ({p.stat().st_size} bytes)")

        markdowns = sorted(tmp_root.rglob("*.md"))
        if not markdowns:
            print("no .md produced — verification FAILED", file=sys.stderr)
            return 1

        md = markdowns[0]
        print(f"\n--- first 800 chars of {md.name} ---")
        print(md.read_text(encoding="utf-8")[:800])
        print("---")

    print("\nverification PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
