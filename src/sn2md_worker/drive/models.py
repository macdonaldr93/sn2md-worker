from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ChangeEvent", "ChangesPage", "ChannelInfo", "FileMetadata"]


class FileMetadata(BaseModel):
    """Subset of a Drive files resource we care about."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    id: str
    name: str
    md5_checksum: str | None = Field(default=None, alias="md5Checksum")
    size: int | None = None
    parents: tuple[str, ...] = ()
    mime_type: str | None = Field(default=None, alias="mimeType")
    trashed: bool = False
    modified_time: datetime | None = Field(default=None, alias="modifiedTime")


class ChangeEvent(BaseModel):
    """A single entry from changes.list."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    file_id: str = Field(alias="fileId")
    removed: bool = False
    time: datetime | None = None
    file: FileMetadata | None = None


class ChangesPage(BaseModel):
    """One page of a changes.list response."""

    model_config = ConfigDict(populate_by_name=True, frozen=True, extra="ignore")

    changes: tuple[ChangeEvent, ...] = ()
    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    new_start_page_token: str | None = Field(default=None, alias="newStartPageToken")


class ChannelInfo(BaseModel):
    """Persistent handle for a Drive push-notification channel."""

    model_config = ConfigDict(frozen=True)

    id: str
    resource_id: str
    expiration: datetime
    token: str
