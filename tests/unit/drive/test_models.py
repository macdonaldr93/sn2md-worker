from __future__ import annotations

from datetime import UTC, datetime

from sn2md_worker.drive.models import ChangeEvent, ChangesPage, FileMetadata


def test_file_metadata_maps_drive_api_aliases() -> None:
    raw = {
        "id": "abc123",
        "name": "20260616_203930.note",
        "md5Checksum": "fea04a85caea47dacbe9fe9358765663",
        "size": "586277",
        "parents": ["parent1"],
        "mimeType": "application/octet-stream",
        "trashed": False,
        "modifiedTime": "2026-07-04T18:31:10.812Z",
    }

    meta = FileMetadata.model_validate(raw)

    assert meta.id == "abc123"
    assert meta.name == "20260616_203930.note"
    assert meta.md5_checksum == "fea04a85caea47dacbe9fe9358765663"
    assert meta.size == 586_277
    assert meta.parents == ("parent1",)
    assert meta.mime_type == "application/octet-stream"
    assert meta.trashed is False
    assert meta.modified_time == datetime(2026, 7, 4, 18, 31, 10, 812_000, tzinfo=UTC)


def test_file_metadata_defaults_are_permissive() -> None:
    meta = FileMetadata.model_validate({"id": "abc", "name": "x.note"})

    assert meta.md5_checksum is None
    assert meta.size is None
    assert meta.parents == ()
    assert meta.trashed is False


def test_file_metadata_ignores_unknown_fields() -> None:
    meta = FileMetadata.model_validate(
        {"id": "abc", "name": "x.note", "webViewLink": "https://example.com"}
    )

    assert meta.id == "abc"


def test_changes_page_maps_change_shape() -> None:
    raw = {
        "nextPageToken": None,
        "newStartPageToken": "42",
        "changes": [
            {
                "fileId": "file-1",
                "removed": False,
                "time": "2026-07-04T18:31:15.770Z",
                "file": {
                    "id": "file-1",
                    "name": "note.note",
                    "md5Checksum": "abc",
                    "mimeType": "application/octet-stream",
                },
            },
            {"fileId": "file-2", "removed": True, "time": "2026-07-04T18:31:20.000Z"},
        ],
    }

    page = ChangesPage.model_validate(raw)

    assert page.new_start_page_token == "42"
    assert page.next_page_token is None
    assert len(page.changes) == 2

    add: ChangeEvent = page.changes[0]
    assert add.file_id == "file-1"
    assert add.removed is False
    assert add.file is not None
    assert add.file.md5_checksum == "abc"

    remove: ChangeEvent = page.changes[1]
    assert remove.file_id == "file-2"
    assert remove.removed is True
    assert remove.file is None
