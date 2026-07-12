from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ListedNote", "NoteMetadata"]


class NoteMetadata(BaseModel):
    """Source-neutral description of a note file.

    Field meanings (id, parents, trashed, mime_type) are defined by each
    source implementation. For Drive they mirror the files resource
    (hence the camelCase aliases); a future local-folder source maps its
    own notions onto the same fields.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    id: str
    name: str
    md5_checksum: str | None = Field(default=None, alias="md5Checksum")
    size: int | None = None
    parents: tuple[str, ...] = ()
    mime_type: str | None = Field(default=None, alias="mimeType")
    trashed: bool = False
    modified_time: datetime | None = Field(default=None, alias="modifiedTime")


@dataclass(frozen=True)
class ListedNote:
    """One note found by `NoteSource.list_all_notes`.

    `source_path` is the source-relative POSIX-ish path (e.g.
    `Notebooks/2026-07.note`) used to compute `logical_key`.
    """

    metadata: NoteMetadata
    source_path: str
