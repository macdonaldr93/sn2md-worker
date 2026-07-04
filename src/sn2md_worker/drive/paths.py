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

    Returns `None` if the file is not a descendant of the given root or
    if the chain exceeds `MAX_PATH_DEPTH`. Returns `""` for the root
    folder itself.
    """
    if file_id == root_folder_id:
        return ""

    segments: list[str] = []
    current_id = file_id
    for _ in range(MAX_PATH_DEPTH):
        meta = get_metadata(current_id)
        segments.append(meta.name)
        if not meta.parents:
            return None
        parent = meta.parents[0]
        if parent == root_folder_id:
            return "/".join(reversed(segments))
        current_id = parent
    return None
