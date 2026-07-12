"""The seam between workflows and wherever .note files come from.

`NoteSource` is the read-only contract for enumerating, inspecting, and
fetching note files. `DriveClient` is the only implementation today; the
seam exists so a local-folder source can slot in later as a config
choice without the workflows knowing which backend they're on.

Implementations surface failures through the neutral exceptions below so
callers can pick a retry strategy without backend-specific catches.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from sn2md_worker.sources.models import ListedNote, NoteMetadata

__all__ = [
    "NoteSource",
    "SourceError",
    "SourcePermanentError",
    "SourceTransientError",
]


class SourceError(Exception):
    """Base for all note-source failures.

    Catch this where any source failure gets the same treatment (the
    log-and-continue boot steps). Callers that react differently to
    permanent vs transient failures should catch `SourcePermanentError`
    or `SourceTransientError` specifically.
    """


class SourcePermanentError(SourceError):
    """Resource gone or request rejected; retrying will not help.

    The condition won't change on retry, so callers can log-and-skip and
    move on to whatever's next instead of stalling the pipeline.
    """


class SourceTransientError(SourceError):
    """Temporary failure; safe for the caller (or DBOS) to retry.

    The condition is expected to clear with time (backoff, rate-limit
    window expiring, network recovery). Callers should let this
    propagate so the surrounding DBOS workflow retries from its
    checkpoint rather than silently dropping the work.
    """


@runtime_checkable
class NoteSource(Protocol):
    """Read-only access to the tree of .note files a source holds."""

    def list_all_notes(self, folder_id: str) -> Iterator[ListedNote]:
        """Walk the tree rooted at `folder_id` and yield every live .note.

        Yields one `ListedNote` per non-trashed file whose name ends in
        `.note` (case-insensitive); folders and other file types are
        skipped. `source_path` is relative to `folder_id`. Listing
        failures raise `SourcePermanentError` (rejected / gone) or
        `SourceTransientError` (temporary; retry-safe).
        """
        ...

    def get_metadata(self, file_id: str) -> NoteMetadata:
        """Return metadata for a single file.

        Raises `SourcePermanentError` if the file is gone or the request
        was rejected, `SourceTransientError` on temporary failures.
        """
        ...

    def download(self, file_id: str, dest_dir: Path, name: str) -> Path:
        """Fetch the file's content into `dest_dir/name`; return that path.

        Creates `dest_dir` if needed. Raises `SourcePermanentError` if
        the file is gone or the request was rejected,
        `SourceTransientError` once temporary failures exhaust any
        internal retries.
        """
        ...

    def find_live_note(self, parent_folder_id: str, name: str) -> NoteMetadata | None:
        """Return a live (non-trashed) file named `name` under `parent_folder_id`.

        Returns `None` when no live match exists, or when `name` contains
        non-printable characters (refused as unsafe before any lookup).
        Lookup failures raise `SourcePermanentError` or
        `SourceTransientError`.
        """
        ...
