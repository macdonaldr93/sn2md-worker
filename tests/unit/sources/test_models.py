from __future__ import annotations

from datetime import UTC, datetime

from sn2md_worker.sources.models import NoteMetadata


def test_note_metadata_maps_drive_api_aliases() -> None:
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

    meta = NoteMetadata.model_validate(raw)

    assert meta.id == "abc123"
    assert meta.name == "20260616_203930.note"
    assert meta.md5_checksum == "fea04a85caea47dacbe9fe9358765663"
    assert meta.size == 586_277
    assert meta.parents == ("parent1",)
    assert meta.mime_type == "application/octet-stream"
    assert meta.trashed is False
    assert meta.modified_time == datetime(2026, 7, 4, 18, 31, 10, 812_000, tzinfo=UTC)


def test_note_metadata_defaults_are_permissive() -> None:
    meta = NoteMetadata.model_validate({"id": "abc", "name": "x.note"})

    assert meta.md5_checksum is None
    assert meta.size is None
    assert meta.parents == ()
    assert meta.trashed is False


def test_note_metadata_ignores_unknown_fields() -> None:
    meta = NoteMetadata.model_validate(
        {"id": "abc", "name": "x.note", "webViewLink": "https://example.com"}
    )

    assert meta.id == "abc"
