"""Pure helpers for computing paths within the shared Drive folder tree."""

from __future__ import annotations

from collections.abc import Callable

from sn2md_worker.drive.models import FileMetadata

__all__ = ["MAX_PATH_DEPTH", "resolve_source_path"]

MAX_PATH_DEPTH = 100


def resolve_source_path(
    *,
    file_id: str,
    root_folder_id: str,
    get_metadata: Callable[[str], FileMetadata],
) -> str | None:
    """Return the file's POSIX path relative to `root_folder_id`.

    Returns `None` if the file is not a descendant of the given root
    (through any parent chain) or if the chain exceeds `MAX_PATH_DEPTH`.
    Returns `""` for the root folder itself.

    Handles legacy multi-parent files: Drive's v3 API preserves multiple
    parents on files added to more than one folder in the v2 era. We try
    each parent in order and return the first chain that reaches the
    root, so a stray parent that leads outside our source tree doesn't
    cause the whole resolution to fail.
    """
    if file_id == root_folder_id:
        return ""
    return _resolve(file_id, root_folder_id, get_metadata, remaining_depth=MAX_PATH_DEPTH)


def _resolve(
    file_id: str,
    root_folder_id: str,
    get_metadata: Callable[[str], FileMetadata],
    remaining_depth: int,
) -> str | None:
    if remaining_depth <= 0:
        return None
    if file_id == root_folder_id:
        return ""

    meta = get_metadata(file_id)
    if not meta.parents:
        return None

    for parent in meta.parents:
        parent_path = _resolve(parent, root_folder_id, get_metadata, remaining_depth - 1)
        if parent_path is None:
            continue
        return f"{parent_path}/{meta.name}" if parent_path else meta.name
    return None
