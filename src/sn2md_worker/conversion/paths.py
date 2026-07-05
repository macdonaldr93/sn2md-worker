"""Pure helpers for mapping a Drive-relative note path into vault paths.

`drive_source_path` is always relative to the configured source folder,
uses forward slashes, and ends with a `.note` extension. Everything here
is stateless — no I/O, no config.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

__all__ = [
    "basename",
    "logical_key",
    "note_output_dir",
    "output_rel_path",
    "sn2md_output_dir",
]


def logical_key(drive_source_path: str) -> str:
    """Stable identity for a note: normalized POSIX path within the source folder."""
    return _normalize(drive_source_path)


def basename(drive_source_path: str) -> str:
    """The note's filename without the `.note` extension."""
    return PurePosixPath(_normalize(drive_source_path)).stem


def sn2md_output_dir(drive_source_path: str, vault_root: Path) -> Path:
    """Directory to pass to sn2md's `output=` argument.

    sn2md writes `<return_value>/<basename>/<basename>.md` (plus assets)
    inside, so this is the *parent* of the per-note subdirectory.
    """
    parent = PurePosixPath(_normalize(drive_source_path)).parent
    if str(parent) in ("", "."):
        return vault_root
    return vault_root / str(parent)


def note_output_dir(drive_source_path: str, vault_root: Path) -> Path:
    """The per-note folder on disk: `<vault>/<parent-dirs>/<basename>/`.

    This is where the multi-page runner writes `page-01.md`, `page-01.png`,
    `index.md`, etc. Concretely: `sn2md_output_dir(...) / basename(...)`.
    """
    return sn2md_output_dir(drive_source_path, vault_root) / basename(drive_source_path)


def output_rel_path(drive_source_path: str) -> str:
    """POSIX path (relative to vault root) of the per-note subdirectory.

    Suitable for persistence in `conversion_records.output_rel_path`.
    """
    normalized = _normalize(drive_source_path)
    parent = PurePosixPath(normalized).parent
    stem = PurePosixPath(normalized).stem
    if str(parent) in ("", "."):
        return stem
    return f"{parent}/{stem}"


def _normalize(drive_source_path: str) -> str:
    return drive_source_path.replace("\\", "/").strip().strip("/")
