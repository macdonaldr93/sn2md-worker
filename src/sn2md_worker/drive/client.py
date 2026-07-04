from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from sn2md_worker.drive.models import ChangesPage, ChannelInfo, FileMetadata

__all__ = [
    "DEFAULT_CHANGES_FIELDS",
    "DEFAULT_FILE_FIELDS",
    "DEFAULT_SCOPES",
    "DriveClient",
    "DriveClientError",
    "get_drive_client",
    "set_drive_client",
]

DEFAULT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",)

DEFAULT_FILE_FIELDS = "id,name,md5Checksum,size,parents,mimeType,trashed,modifiedTime"
DEFAULT_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,time,"
    "file(id,name,md5Checksum,parents,mimeType,trashed,modifiedTime))"
)


class DriveClientError(RuntimeError):
    """Raised when a Drive API call fails or credentials are misconfigured."""


class DriveClient:
    """Thin wrapper around google-api-python-client's Drive v3 service."""

    def __init__(
        self,
        credentials_path: Path,
        scopes: Sequence[str] = DEFAULT_SCOPES,
    ) -> None:
        if not credentials_path.is_file():
            raise DriveClientError(f"credentials file not found: {credentials_path}")

        credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(credentials_path), scopes=list(scopes)
        )
        self._service: Any = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._service_account_email: str = getattr(
            credentials, "service_account_email", "<unknown>"
        )

    @property
    def service_account_email(self) -> str:
        return self._service_account_email

    def get_metadata(self, file_id: str, fields: str = DEFAULT_FILE_FIELDS) -> FileMetadata:
        raw = self._call(lambda: self._service.files().get(fileId=file_id, fields=fields).execute())
        return FileMetadata.model_validate(raw)

    def get_start_page_token(self) -> str:
        raw = self._call(
            lambda: self._service.changes().getStartPageToken(supportsAllDrives=False).execute()
        )
        token = raw.get("startPageToken")
        if not isinstance(token, str):
            raise DriveClientError(f"getStartPageToken returned unexpected shape: {raw!r}")
        return token

    def download(self, file_id: str, dest_dir: Path, name: str) -> Path:
        """Download a Drive file's content to `dest_dir/name`. Returns the path."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        try:
            content = self._service.files().get_media(fileId=file_id).execute()
        except HttpError as exc:
            raise DriveClientError(f"files.get_media failed: {exc}") from exc
        if not isinstance(content, bytes):
            raise DriveClientError(
                f"unexpected Drive media response type: {type(content).__name__}"
            )
        target.write_bytes(content)
        return target

    def find_live_note(self, parent_folder_id: str, name: str) -> FileMetadata | None:
        """Return a live (non-trashed) file in the given folder matching `name`.

        Used by delete_output to detect Supernote's replace-then-delete
        pattern: if the delete event is for an old file_id but a new file
        with the same name already exists at the same location, we
        re-point rather than nuke the vault output.
        """
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and trashed = false " f"and '{parent_folder_id}' in parents"
        raw = self._call(
            lambda: self._service.files()
            .list(
                q=query,
                fields=f"files({DEFAULT_FILE_FIELDS})",
                pageSize=2,
                spaces="drive",
                supportsAllDrives=False,
                includeItemsFromAllDrives=False,
            )
            .execute()
        )
        files = raw.get("files", [])
        if not files:
            return None
        return FileMetadata.model_validate(files[0])

    def watch_changes(
        self,
        *,
        webhook_url: str,
        channel_id: str,
        token: str,
        start_page_token: str,
        ttl_seconds: int,
    ) -> ChannelInfo:
        """Create a push-notification channel for changes.list.

        Google enforces a maximum TTL of 7 days (604800s) on changes
        channels; requesting more is silently capped.
        """
        expiration_ms = int(time.time() * 1000) + ttl_seconds * 1000
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": webhook_url,
            "token": token,
            "expiration": expiration_ms,
        }
        raw = self._call(
            lambda: self._service.changes()
            .watch(
                pageToken=start_page_token,
                includeRemoved=True,
                restrictToMyDrive=False,
                spaces="drive",
                supportsAllDrives=False,
                body=body,
            )
            .execute()
        )
        expiration = datetime.fromtimestamp(int(raw["expiration"]) / 1000, tz=UTC)
        return ChannelInfo(
            id=raw["id"],
            resource_id=raw["resourceId"],
            expiration=expiration,
            token=token,
        )

    def changes_list(
        self,
        page_token: str,
        *,
        include_removed: bool = True,
        fields: str = DEFAULT_CHANGES_FIELDS,
        page_size: int = 100,
    ) -> ChangesPage:
        raw = self._call(
            lambda: self._service.changes()
            .list(
                pageToken=page_token,
                includeRemoved=include_removed,
                restrictToMyDrive=False,
                spaces="drive",
                fields=fields,
                supportsAllDrives=False,
                pageSize=page_size,
            )
            .execute()
        )
        return ChangesPage.model_validate(raw)

    @staticmethod
    def _call(action: Any) -> dict[str, Any]:
        try:
            result = action()
        except HttpError as exc:
            raise DriveClientError(str(exc)) from exc
        if not isinstance(result, dict):
            raise DriveClientError(f"unexpected Drive response type: {type(result).__name__}")
        return result


def get_drive_client() -> DriveClient:
    """Return the process-wide DriveClient; raises if not yet initialized."""
    if _Holder.client is None:
        raise RuntimeError("drive client not initialized; call set_drive_client() at startup")
    return _Holder.client


def set_drive_client(client: DriveClient) -> None:
    """Install the process-wide DriveClient. Call once from the entrypoint."""
    _Holder.client = client


class _Holder:
    client: DriveClient | None = None
