from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from sn2md_worker.drive.models import ChangesPage, FileMetadata

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
