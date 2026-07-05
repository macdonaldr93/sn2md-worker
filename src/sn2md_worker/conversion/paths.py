"""Pure helpers for mapping a Drive-relative note path into vault paths.

`drive_source_path` is always relative to the configured source folder,
uses forward slashes, and ends with a `.note` extension. Everything here
is stateless — no I/O, no config.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

__all__ = [
    "UnsafePathError",
    "basename",
    "logical_key",
    "note_output_dir",
    "output_rel_path",
    "sn2md_output_dir",
]


class UnsafePathError(ValueError):
    """Raised when a Drive-derived path contains components that would
    let vault writes escape the vault root or land on Windows-reserved
    filenames. Callers should log-and-skip the affected note.
    """


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
    cleaned = drive_source_path.replace("\\", "/").strip().strip("/")
    if not cleaned:
        raise UnsafePathError("empty drive source path")
    parts = cleaned.split("/")
    for part in parts:
        _reject_unsafe_component(part)
    return cleaned


# CON/PRN/AUX/NUL/COM1-9/LPT1-9 are reserved on Windows regardless of
# extension. We reject them so a note synced from a Supernote to a
# vault that ever lives on Windows/exFAT doesn't produce ghost files.
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _reject_unsafe_component(part: str) -> None:
    if not part:
        raise UnsafePathError("empty path component (double-slash in source path)")
    if part in (".", ".."):
        raise UnsafePathError(f"path component {part!r} would traverse outside the vault")
    if "\x00" in part:
        raise UnsafePathError(f"path component {part!r} contains NUL")
    # Split on '.' and check the stem so `CON.note` is caught, not just `CON`.
    stem = part.split(".", 1)[0]
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        raise UnsafePathError(f"path component {part!r} is a Windows-reserved filename")
