from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth.exceptions import MalformedError, RefreshError
from googleapiclient.errors import HttpError
from tenacity import wait_none

from sn2md_worker.drive import client as client_module
from sn2md_worker.drive.client import (
    DriveClient,
    DriveClientError,
    DrivePermanentError,
    DriveTransientError,
)


@pytest.fixture
def instant_drive_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-out tenacity's backoff sleeps so retry tests run fast."""
    monkeypatch.setattr(client_module._invoke_with_drive_retry.retry, "wait", wait_none())
    monkeypatch.setattr(client_module._download_media_with_retry.retry, "wait", wait_none())


def _http_error(status: int, body: bytes = b"{}") -> HttpError:
    return HttpError(httplib2.Response({"status": status}), body)


def _drive_client_with_service(service: Any) -> DriveClient:
    """Build a DriveClient bypassing credentials/service construction."""
    inst = DriveClient.__new__(DriveClient)
    inst._service = service  # type: ignore[attr-defined]
    inst._service_account_email = "test@example.com"  # type: ignore[attr-defined]
    inst._credentials = MagicMock()  # type: ignore[attr-defined]
    inst._thread_local = threading.local()  # type: ignore[attr-defined]
    return inst


class TestDriveCallOn5xxError:
    def test_retries_and_returns_after_transient_failure(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN — a 500 then success
        call_counter = {"n": 0}

        def action() -> dict[str, Any]:
            call_counter["n"] += 1
            if call_counter["n"] < 2:
                raise _http_error(500)
            return {"ok": True}

        # WHEN
        result = DriveClient._call(action)

        # THEN
        assert call_counter["n"] == 2
        assert result == {"ok": True}


class TestDriveCallOn429RateLimit:
    def test_retries_the_call(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN
        call_counter = {"n": 0}

        def action() -> dict[str, Any]:
            call_counter["n"] += 1
            if call_counter["n"] < 2:
                raise _http_error(429)
            return {"ok": True}

        # WHEN / THEN
        DriveClient._call(action)
        assert call_counter["n"] == 2


class TestDriveCallOn4xxError:
    def test_does_not_retry_and_wraps_in_drive_client_error(
        self, instant_drive_retries: None
    ) -> None:  # noqa: ARG002
        # GIVEN
        call_counter = {"n": 0}

        def action() -> dict[str, Any]:
            call_counter["n"] += 1
            raise _http_error(400)

        # WHEN / THEN — 400 is permanent, no retry, wrapped
        with pytest.raises(DriveClientError):
            DriveClient._call(action)
        assert call_counter["n"] == 1


class TestDriveCallOnNetworkTimeout:
    def test_retries_and_wraps_after_exhaustion(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN
        call_counter = {"n": 0}

        def action() -> dict[str, Any]:
            call_counter["n"] += 1
            raise TimeoutError("read timed out")

        # WHEN / THEN — retried up to max attempts, then wrapped.
        # socket.timeout is aliased to TimeoutError in Python 3.10+, so
        # the wrapped message reports the TimeoutError type name.
        with pytest.raises(DriveClientError, match="TimeoutError"):
            DriveClient._call(action)
        assert call_counter["n"] == 3  # _DRIVE_MAX_ATTEMPTS


class TestDriveCallOnDnsFailure:
    def test_retries_httplib2_server_not_found(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN
        call_counter = {"n": 0}

        def action() -> dict[str, Any]:
            call_counter["n"] += 1
            raise httplib2.ServerNotFoundError("unable to resolve googleapis.com")

        # WHEN / THEN
        with pytest.raises(DriveClientError, match="ServerNotFoundError"):
            DriveClient._call(action)
        assert call_counter["n"] == 3


class _FakeDownloaderFactory:
    """Build a MediaIoBaseDownload stand-in configurable per test.

    Each attempt gets a fresh instance from `__call__`. `next_chunk` runs
    the caller-supplied action first (raise / write / …) so a test can
    fail the first attempt, succeed on the second, etc.
    """

    def __init__(
        self,
        *,
        chunks_per_attempt: list[list[Any]],
    ) -> None:
        # chunks_per_attempt[i] is the sequence of actions for the i-th
        # top-level call. Each action is either `bytes` (written) or an
        # `Exception` (raised).
        self._plans = list(chunks_per_attempt)
        self.attempts = 0

    def __call__(self, fh: Any, request: Any, chunksize: int) -> Any:  # noqa: ARG002
        plan = self._plans.pop(0)
        return _FakeDownloader(fh=fh, plan=plan)


class _FakeDownloader:
    def __init__(self, *, fh: Any, plan: list[Any]) -> None:
        self._fh = fh
        self._plan = plan

    def next_chunk(self) -> tuple[None, bool]:
        action = self._plan.pop(0)
        if isinstance(action, Exception):
            raise action
        self._fh.write(action)
        done = not self._plan
        return (None, done)


class TestDownloadHappyPath:
    def test_streams_content_to_target_and_returns_path(self, tmp_path: Path) -> None:
        # GIVEN — one attempt of two chunks
        service = MagicMock()
        drive = _drive_client_with_service(service)
        factory = _FakeDownloaderFactory(
            chunks_per_attempt=[[b"part-1-", b"part-2"]],
        )

        # WHEN
        with patch("sn2md_worker.drive.client.MediaIoBaseDownload", factory):
            path = drive.download("file-1", tmp_path, "note.note")

        # THEN
        assert path == tmp_path / "note.note"
        assert path.read_bytes() == b"part-1-part-2"


class TestDownloadOn5xxDuringChunks:
    def test_retries_and_writes_full_content_from_the_successful_attempt(
        self, tmp_path: Path, instant_drive_retries: None
    ) -> None:  # noqa: ARG002
        # GIVEN — attempt 1 writes half then 500s; attempt 2 succeeds fully
        service = MagicMock()
        drive = _drive_client_with_service(service)
        factory = _FakeDownloaderFactory(
            chunks_per_attempt=[
                [b"half-", _http_error(500)],
                [b"full-content"],
            ],
        )

        # WHEN
        with patch("sn2md_worker.drive.client.MediaIoBaseDownload", factory):
            path = drive.download("file-1", tmp_path, "note.note")

        # THEN — retry truncated the "half-" bytes from attempt 1
        assert path.read_bytes() == b"full-content"


class TestDownloadOn400PermanentError:
    def test_does_not_retry_and_wraps_in_drive_client_error(
        self, tmp_path: Path, instant_drive_retries: None
    ) -> None:  # noqa: ARG002
        # GIVEN — 400 on first attempt
        service = MagicMock()
        drive = _drive_client_with_service(service)
        attempts = {"n": 0}

        def make_downloader(fh: Any, request: Any, chunksize: int) -> Any:  # noqa: ARG001
            attempts["n"] += 1
            return _FakeDownloader(fh=fh, plan=[_http_error(400)])

        # WHEN / THEN
        with (
            patch("sn2md_worker.drive.client.MediaIoBaseDownload", make_downloader),
            pytest.raises(DriveClientError),
        ):
            drive.download("file-1", tmp_path, "note.note")

        # 400 is permanent — one attempt only
        assert attempts["n"] == 1


class TestListChildrenPagination:
    def test_yields_across_pages_and_uses_retryable_call(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN — two-page listing, first execute() 500s once
        pages = [
            {
                "files": [
                    {
                        "id": "a",
                        "name": "a.note",
                        "mimeType": "application/octet-stream",
                        "trashed": False,
                    }
                ],
                "nextPageToken": "PAGE2",
            },
            {
                "files": [
                    {
                        "id": "b",
                        "name": "b.note",
                        "mimeType": "application/octet-stream",
                        "trashed": False,
                    }
                ],
            },
        ]
        seen = {"executes": 0}

        def execute() -> dict[str, Any]:
            seen["executes"] += 1
            if seen["executes"] == 1:
                raise _http_error(503)
            return pages.pop(0)

        list_call = MagicMock()
        list_call.execute = execute
        files_mock = MagicMock()
        files_mock.list.return_value = list_call
        service = MagicMock()
        service.files.return_value = files_mock

        drive = _drive_client_with_service(service)

        # WHEN
        seen_ids = [c.id for c in drive._list_children("root")]

        # THEN — one retried 503 plus two real page loads = 3 execute() calls
        assert seen_ids == ["a", "b"]
        assert seen["executes"] == 3


class TestListChildrenDeduplicatesRepeatedIds:
    def test_yields_each_file_id_at_most_once_across_pages(self) -> None:
        # GIVEN — two pages where "a" appears on BOTH (a concurrent Drive
        # edit shifted pagination ordering so page 2 revisits an item)
        pages = [
            {
                "files": [
                    {"id": "a", "name": "a.note", "mimeType": "x", "trashed": False},
                    {"id": "b", "name": "b.note", "mimeType": "x", "trashed": False},
                ],
                "nextPageToken": "PAGE2",
            },
            {
                "files": [
                    {"id": "a", "name": "a.note", "mimeType": "x", "trashed": False},
                    {"id": "c", "name": "c.note", "mimeType": "x", "trashed": False},
                ],
            },
        ]

        def execute() -> dict[str, Any]:
            return pages.pop(0)

        list_call = MagicMock()
        list_call.execute = execute
        files_mock = MagicMock()
        files_mock.list.return_value = list_call
        service = MagicMock()
        service.files.return_value = files_mock

        drive = _drive_client_with_service(service)

        # WHEN
        seen_ids = [c.id for c in drive._list_children("root")]

        # THEN — "a" appears once even though Drive returned it on both pages
        assert seen_ids == ["a", "b", "c"]


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "note\nnewline.note",
        "note\rcarriage.note",
        "note\ttab.note",
        "note\x00nul.note",
        "note\x1bescape.note",
        "note\x7fdelete.note",
    ],
)
class TestFindLiveNoteRejectsControlChars:
    def test_returns_none_without_querying_drive(self, unsafe_name: str) -> None:
        # GIVEN
        service = MagicMock()
        drive = _drive_client_with_service(service)

        # WHEN
        result = drive.find_live_note("parent-1", unsafe_name)

        # THEN — refused before any Drive call
        assert result is None
        service.files.assert_not_called()


class TestFindLiveNoteAcceptsNormalNames:
    def test_queries_drive_and_returns_the_matching_file(self) -> None:
        # GIVEN — Drive returns one file
        list_call = MagicMock()
        list_call.execute.return_value = {
            "files": [
                {
                    "id": "file-1",
                    "name": "note.note",
                    "mimeType": "application/octet-stream",
                    "trashed": False,
                    "parents": ["parent-1"],
                }
            ]
        }
        files_mock = MagicMock()
        files_mock.list.return_value = list_call
        service = MagicMock()
        service.files.return_value = files_mock
        drive = _drive_client_with_service(service)

        # WHEN
        result = drive.find_live_note("parent-1", "note.note")

        # THEN
        assert result is not None
        assert result.id == "file-1"


class TestGetHttpIsPerThread:
    def test_each_thread_gets_its_own_authorized_http(self) -> None:
        # GIVEN — one DriveClient shared across worker threads (production
        # setup: `_Holder.client` is a process-wide singleton, DBOS runs
        # workflows on a thread pool).
        service = MagicMock()
        drive = _drive_client_with_service(service)

        # WHEN — many threads race to grab their http
        def _grab() -> tuple[int, int]:
            http = drive._get_http()
            # Return (thread ident, id(http)) so we can assert one-to-one.
            return threading.get_ident(), id(http)

        with ThreadPoolExecutor(max_workers=8) as pool:
            observed = list(pool.map(lambda _: _grab(), range(64)))

        # THEN — every distinct thread holds a distinct AuthorizedHttp, and
        # repeated calls from the same thread return the same instance
        # (the whole point of caching in threading.local).
        by_thread: dict[int, set[int]] = {}
        for thread_id, http_id in observed:
            by_thread.setdefault(thread_id, set()).add(http_id)

        assert all(
            len(hs) == 1 for hs in by_thread.values()
        ), "same thread returned multiple Http instances"
        all_http_ids = {next(iter(hs)) for hs in by_thread.values()}
        assert len(all_http_ids) == len(
            by_thread
        ), "different threads shared the same Http instance — not thread-safe"


class TestDriveClientConstructorWrapsMalformedKey:
    def test_malformed_json_surfaces_as_drive_client_error(self, tmp_path: Path) -> None:
        # GIVEN — a file that exists but isn't a valid service-account JSON
        creds = tmp_path / "sa.json"
        creds.write_text("{}")

        # WHEN / THEN — google-auth raises MalformedError; DriveClient wraps
        # as DriveClientError so callers only need one exception class.
        with pytest.raises(DriveClientError, match="invalid credentials file"):
            DriveClient(creds)

    def test_missing_file_still_raises_drive_client_error(self, tmp_path: Path) -> None:
        # GIVEN — path doesn't exist
        creds = tmp_path / "missing.json"

        # WHEN / THEN — pre-check raises before google-auth is called
        with pytest.raises(DriveClientError, match="credentials file not found"):
            DriveClient(creds)


class TestDriveCallOnRefreshError:
    def test_wraps_as_transient(self, instant_drive_retries: None) -> None:  # noqa: ARG002
        # GIVEN — token refresh fails (bad key OR network to oauth2.googleapis.com)
        def action() -> dict[str, Any]:
            raise RefreshError("invalid_grant: Invalid grant: account not found")

        # WHEN / THEN — treated as transient per the boundary contract:
        # Google doesn't distinguish transient network from permanent bad-key
        # cleanly, so we degrade at boot instead of crashing.
        with pytest.raises(DriveTransientError, match="RefreshError"):
            DriveClient._call(action)

    def test_generic_google_auth_error_also_wraps_as_transient(
        self, instant_drive_retries: None
    ) -> None:  # noqa: ARG002
        # GIVEN — a broader GoogleAuthError subclass
        def action() -> dict[str, Any]:
            raise MalformedError("missing scope")

        # WHEN / THEN
        with pytest.raises(DriveTransientError, match="MalformedError"):
            DriveClient._call(action)


class TestDriveErrorFromHttpClassification:
    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_4xx_maps_to_permanent(self, status: int) -> None:
        err = client_module._drive_error_from_http(_http_error(status))
        assert isinstance(err, DrivePermanentError)
        assert isinstance(err, DriveClientError)

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_429_and_5xx_map_to_transient(self, status: int) -> None:
        err = client_module._drive_error_from_http(_http_error(status))
        assert isinstance(err, DriveTransientError)
        assert not isinstance(err, DrivePermanentError)
