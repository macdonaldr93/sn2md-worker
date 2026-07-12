from __future__ import annotations

from sn2md_worker.drive.models import ChangeEvent, ChangesPage


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
