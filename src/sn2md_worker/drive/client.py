from __future__ import annotations

import ssl
import threading
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest, MediaIoBaseDownload
from httplib2 import ServerNotFoundError
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from sn2md_worker.drive.models import ChangesPage, ChannelInfo, FileMetadata
from sn2md_worker.logging import get_logger

__all__ = [
    "DEFAULT_CHANGES_FIELDS",
    "DEFAULT_FILE_FIELDS",
    "DEFAULT_SCOPES",
    "DRIVE_FOLDER_MIME",
    "DriveClient",
    "DriveClientError",
    "get_drive_client",
    "set_drive_client",
]

DEFAULT_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",)

_log = get_logger("sn2md_worker.drive.client")

_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_SERVER_ERROR_MAX_EXCLUSIVE = 600

# Retry budget: 3 attempts with exponential backoff + jitter, bounded
# well under a minute in the worst case. Transient conditions retried:
# HTTP 429 (rate-limited), 5xx (server), plus network-level exceptions
# that googleapiclient does not wrap. Permanent 4xx (400/401/403/404) are
# not retried — those want to surface immediately so callers can decide.
_DRIVE_MAX_ATTEMPTS = 3
_DRIVE_BACKOFF_INITIAL_SECONDS = 2
_DRIVE_BACKOFF_MAX_SECONDS = 20

# 4 MB chunks — big enough that a typical Supernote `.note` (single-digit
# MB) downloads in one round trip, small enough that we never buffer a
# runaway file in memory. `MediaIoBaseDownload` requests each chunk via
# HTTP Range so the process memory ceiling is one chunksize regardless of
# the file's total size — the whole point of switching off `.execute()`.
_DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024

DEFAULT_FILE_FIELDS = "id,name,md5Checksum,size,parents,mimeType,trashed,modifiedTime"
DEFAULT_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,time,"
    "file(id,name,md5Checksum,parents,mimeType,trashed,modifiedTime))"
)
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
_NOTE_EXTENSION = ".note"


class DriveClientError(RuntimeError):
    """Raised when a Drive API call fails or credentials are misconfigured.

    Callers that need to react differently to permanent vs. transient
    failures (`poll_changes` uses this to decide whether to raise or
    log-skip) should catch `DrivePermanentError` or `DriveTransientError`
    specifically. Legacy raises of the plain base class are still used
    for schema/response-shape errors that aren't Drive HTTP failures.
    """


class DrivePermanentError(DriveClientError):
    """A Drive HTTP call failed with a non-retryable 4xx.

    Represents "the resource is gone / rejected / forbidden" — the
    condition won't change if we retry, so callers can log-and-skip and
    move on to whatever's next instead of stalling the pipeline.
    """


class DriveTransientError(DriveClientError):
    """A Drive HTTP call failed with 5xx / 429 / network error after retries.

    The condition is expected to clear with time (backoff, rate-limit
    window expiring, network recovery). Callers should let this
    propagate so the surrounding DBOS workflow retries from its
    checkpoint rather than silently dropping the work.
    """


def _drive_error_from_http(exc: HttpError) -> DriveClientError:
    """Wrap an HttpError as `Permanent` or `Transient` based on status.

    4xx (except 429, which was retried and exhausted → transient by that
    point) are permanent; the resource is gone or the request was
    malformed and re-trying doesn't help. Everything else — no status,
    5xx after retries — is transient and expected to clear.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if (
        isinstance(status, int)
        and _HTTP_CLIENT_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MIN
        and status != _HTTP_TOO_MANY_REQUESTS
    ):
        return DrivePermanentError(str(exc))
    return DriveTransientError(str(exc))


def _is_transient_drive_error(exc: BaseException) -> bool:
    """True for errors worth retrying: 429, 5xx, or network-level failures."""
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == _HTTP_TOO_MANY_REQUESTS:
            return True
        return (
            isinstance(status, int)
            and _HTTP_SERVER_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MAX_EXCLUSIVE
        )
    # socket.timeout is aliased to TimeoutError on Py3.10+, so TimeoutError
    # covers both. ConnectionError catches ECONNRESET/ECONNREFUSED. Do NOT
    # retry every OSError — a local-file permission issue shouldn't loop.
    return isinstance(exc, ServerNotFoundError | ssl.SSLError | TimeoutError | ConnectionError)


def _log_drive_retry(retry_state: RetryCallState) -> None:
    """tenacity `before_sleep` hook — one structured warning per backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
    next_wait = retry_state.next_action.sleep if retry_state.next_action is not None else 0
    status = None
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
    _log.warning(
        "drive_call_retry_scheduled",
        attempt=retry_state.attempt_number,
        max_attempts=_DRIVE_MAX_ATTEMPTS,
        next_wait_seconds=round(next_wait, 2),
        error_type=type(exc).__name__ if exc is not None else None,
        http_status=status,
    )


@retry(
    stop=stop_after_attempt(_DRIVE_MAX_ATTEMPTS),
    wait=wait_exponential_jitter(
        initial=_DRIVE_BACKOFF_INITIAL_SECONDS, max=_DRIVE_BACKOFF_MAX_SECONDS
    ),
    retry=retry_if_exception(_is_transient_drive_error),
    before_sleep=_log_drive_retry,
    reraise=True,
)
def _invoke_with_drive_retry(action: Any) -> Any:
    """Run a Drive API `.execute()` action with bounded exponential-backoff retry."""
    return action()


@retry(
    stop=stop_after_attempt(_DRIVE_MAX_ATTEMPTS),
    wait=wait_exponential_jitter(
        initial=_DRIVE_BACKOFF_INITIAL_SECONDS, max=_DRIVE_BACKOFF_MAX_SECONDS
    ),
    retry=retry_if_exception(_is_transient_drive_error),
    before_sleep=_log_drive_retry,
    reraise=True,
)
def _download_media_with_retry(*, service: Any, file_id: str, target: Path) -> None:
    """Stream Drive media into `target` via `MediaIoBaseDownload`.

    Rebuilds the `get_media` request on each attempt so the underlying
    `HttpRequest` object never carries state from a failed previous try.
    Opening `target` in `"wb"` mode inside the retry truncates any bytes
    written by a prior partial attempt.
    """
    request = service.files().get_media(fileId=file_id)
    with target.open("wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=_DOWNLOAD_CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk()


class DriveClient:
    """Thin wrapper around google-api-python-client's Drive v3 service."""

    def __init__(
        self,
        credentials_path: Path,
        scopes: Sequence[str] = DEFAULT_SCOPES,
    ) -> None:
        if not credentials_path.is_file():
            raise DriveClientError(f"credentials file not found: {credentials_path}")

        # google-auth raises MalformedError (subclass of GoogleAuthError)
        # for missing fields, ValueError for JSON parse failures, and
        # OSError for read failures. Any of them mean "this file isn't a
        # usable service-account key"; surface as DriveClientError so the
        # boot path can log-and-continue instead of crashing with a bare
        # google-auth traceback.
        try:
            credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(credentials_path), scopes=list(scopes)
            )
        except (GoogleAuthError, ValueError, OSError) as exc:
            raise DriveClientError(
                f"invalid credentials file {credentials_path} ({type(exc).__name__}): {exc}"
            ) from exc
        self._credentials: Any = credentials
        # httplib2.Http is not thread-safe. `requestBuilder` swaps in a
        # per-thread AuthorizedHttp so DBOS worker threads never race on
        # one TLS socket (would surface as `ssl.SSLError: record layer failure`).
        self._thread_local = threading.local()
        self._service: Any = build(
            "drive",
            "v3",
            credentials=credentials,
            requestBuilder=self._build_request,
            cache_discovery=False,
        )
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
        """Download a Drive file's content to `dest_dir/name`. Returns the path.

        Streams the file via `MediaIoBaseDownload` in fixed-size chunks so
        the process memory ceiling is one chunk regardless of file size;
        the previous `.execute()` path returned the full body as bytes and
        could OOM (or silently truncate) on large notes.

        Transient failures (429, 5xx, network) are retried at the whole-
        download level; on retry the file is truncated and re-fetched
        from byte 0.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        try:
            _download_media_with_retry(service=self._service, file_id=file_id, target=target)
        except HttpError as exc:
            raise _drive_error_from_http(exc) from exc
        except GoogleAuthError as exc:
            # Token refresh failed mid-download. Treat as transient — a
            # real credential problem will surface again on the next call
            # and cron-driven retries will bring it back.
            raise DriveTransientError(f"Drive auth failure ({type(exc).__name__}): {exc}") from exc
        except (ServerNotFoundError, ssl.SSLError, TimeoutError, OSError) as exc:
            raise DriveTransientError(
                f"Drive transport failure ({type(exc).__name__}): {exc}"
            ) from exc
        return target

    def list_all_notes(self, folder_id: str) -> Iterator[tuple[FileMetadata, str]]:
        """Walk the folder tree rooted at `folder_id` and yield every .note.

        Each yield is `(file_metadata, source_path)` where `source_path` is
        the POSIX path relative to `folder_id` (e.g. `Notebooks/2026-07.note`
        for a note two levels deep). Folder traversal is depth-first via an
        explicit stack; pagination inside a folder is handled by
        `_list_children`.
        """
        stack: list[tuple[str, str]] = [(folder_id, "")]
        while stack:
            current_id, current_path = stack.pop()
            for child in self._list_children(current_id):
                child_path = f"{current_path}/{child.name}" if current_path else child.name
                if child.mime_type == DRIVE_FOLDER_MIME:
                    stack.append((child.id, child_path))
                elif child.name.lower().endswith(_NOTE_EXTENSION) and not child.trashed:
                    yield (child, child_path)

    def find_live_note(self, parent_folder_id: str, name: str) -> FileMetadata | None:
        """Return a live (non-trashed) file in the given folder matching `name`.

        Detects Supernote's replace-then-delete pattern: if a delete
        event fires for an old file_id but a new file with the same
        name already exists at the same location, callers re-point
        rather than nuke the vault output.

        Rejects non-printable names (NUL, newlines, DEL, Unicode format
        separators, etc.) — Google's query language quotes strings with
        `'`, and embedded control chars can inject fragments or truncate
        the query. Supernote never produces such names, so refusing is
        safe. `str.isprintable()` treats `""` as printable; that's fine
        since an empty name here would resolve to a legitimate no-op.
        """
        if not name.isprintable():
            _log.warning(
                "drive_find_live_note_rejected_unsafe_name",
                reason="control_chars",
                parent_folder_id=parent_folder_id,
            )
            return None
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and trashed = false and '{parent_folder_id}' in parents"
        raw = self._call(
            lambda: (
                self._service.files()
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
        )
        files = raw.get("files", [])
        if not files:
            return None
        return FileMetadata.model_validate(files[0])

    def stop_channel(self, channel_id: str, resource_id: str) -> None:
        """Stop a push-notification channel. Idempotent from the caller's view.

        Google returns 404 when the channel is already gone (expired, or
        stopped previously); we swallow that as success. Other errors
        surface as `DriveClientError` so callers can log-and-continue.
        """
        body = {"id": channel_id, "resourceId": resource_id}
        try:
            self._service.channels().stop(body=body).execute()
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == _HTTP_NOT_FOUND:
                return
            raise _drive_error_from_http(exc) from exc

    def watch_changes(
        self,
        *,
        webhook_url: str,
        channel_id: str,
        token: str,
        start_page_token: str,
    ) -> ChannelInfo:
        """Create a push-notification channel for changes.list.

        We deliberately do NOT send `expiration` — Google's default is the
        maximum TTL (7 days), which is exactly what we want, and computing
        the value locally would depend on the host wall clock (a skewed
        Docker host would silently request a wrong-length channel). The
        actual expiry Google assigns is read back from `raw["expiration"]`
        below and persisted verbatim.
        """
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": webhook_url,
            "token": token,
        }
        raw = self._call(
            lambda: (
                self._service.changes()
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
            lambda: (
                self._service.changes()
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
        )
        return ChangesPage.model_validate(raw)

    def _build_request(self, _shared_http: Any, *args: Any, **kwargs: Any) -> HttpRequest:
        # googleapiclient calls this for every HttpRequest the service builds.
        # We ignore the service's shared http and hand each request this
        # thread's own AuthorizedHttp so calls never race on one TLS socket.
        return HttpRequest(self._get_http(), *args, **kwargs)

    def _get_http(self) -> Any:
        http = getattr(self._thread_local, "http", None)
        if http is None:
            http = google_auth_httplib2.AuthorizedHttp(self._credentials, http=httplib2.Http())
            self._thread_local.http = http
        return http

    def _list_children(self, folder_id: str) -> Iterator[FileMetadata]:
        page_token: str | None = None
        # Drive can return the same file id on two consecutive pages if a
        # concurrent edit shifts pagination ordering mid-walk. Track ids
        # we've yielded so callers see each child at most once per listing.
        seen_ids: set[str] = set()
        while True:
            # Bind page_token via default arg so a retry inside `_call`
            # re-runs with the same token, not a later loop iteration's.
            raw = self._call(
                lambda pt=page_token: (
                    self._service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=f"nextPageToken,files({DEFAULT_FILE_FIELDS})",
                        pageSize=100,
                        spaces="drive",
                        supportsAllDrives=False,
                        includeItemsFromAllDrives=False,
                        pageToken=pt,
                    )
                    .execute()
                )
            )
            for entry in raw.get("files", []):
                file_metadata = FileMetadata.model_validate(entry)
                if file_metadata.id in seen_ids:
                    continue
                seen_ids.add(file_metadata.id)
                yield file_metadata
            page_token = raw.get("nextPageToken")
            if not page_token:
                break

    @staticmethod
    def _call(action: Any) -> dict[str, Any]:
        """Invoke a Drive API `.execute()` with bounded retry + broad transport-error wrapping.

        Retries on transient failures (429, 5xx, socket/SSL/DNS errors)
        via `_invoke_with_drive_retry`. Permanent HTTP errors (4xx except
        429) surface after the first attempt as `DrivePermanentError`;
        exhausted transient errors as `DriveTransientError`. Callers
        distinguish those so `poll_changes` can log-skip permanent (the
        resource is gone) while letting transient stall the whole
        workflow for a DBOS-level retry.
        """
        try:
            result = _invoke_with_drive_retry(action)
        except HttpError as exc:
            raise _drive_error_from_http(exc) from exc
        except GoogleAuthError as exc:
            # `_perform_refresh_token` raises `RefreshError` from inside
            # the call — reasons range from "account not found" (bad key,
            # permanent) to "network to oauth2.googleapis.com timed out"
            # (transient). Google doesn't offer a machine-readable
            # distinction, and string-matching on the message is fragile;
            # treat all as transient. A real bad-key still surfaces on
            # every call within the boot, and the boot path degrades to
            # a live-but-503 container instead of a crashloop.
            raise DriveTransientError(f"Drive auth failure ({type(exc).__name__}): {exc}") from exc
        except (ServerNotFoundError, ssl.SSLError, TimeoutError, OSError) as exc:
            # socket.timeout is TimeoutError on Py3.10+; ConnectionError is
            # an OSError subclass. `except OSError` catches both, plus the
            # occasional low-level socket error httplib2 can bubble up.
            raise DriveTransientError(
                f"Drive transport failure ({type(exc).__name__}): {exc}"
            ) from exc
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
