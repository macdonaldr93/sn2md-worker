from __future__ import annotations

from sn2md_worker.drive.models import FileMetadata
from sn2md_worker.drive.paths import resolve_source_path

ROOT = "ROOT_ID"


def _fake_get_metadata(store: dict[str, FileMetadata]):
    def _lookup(file_id: str) -> FileMetadata:
        return store[file_id]

    return _lookup


def test_file_directly_in_root() -> None:
    store = {"F": FileMetadata(id="F", name="2026-07.note", parents=(ROOT,))}

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result == "2026-07.note"


def test_file_in_nested_subfolders() -> None:
    store = {
        "F": FileMetadata(id="F", name="2026-07.note", parents=("SUB",)),
        "SUB": FileMetadata(id="SUB", name="Journal", parents=("PARENT",)),
        "PARENT": FileMetadata(id="PARENT", name="Notebooks", parents=(ROOT,)),
    }

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result == "Notebooks/Journal/2026-07.note"


def test_root_folder_id_itself_returns_empty_string() -> None:
    result = resolve_source_path(
        file_id=ROOT, root_folder_id=ROOT, get_metadata=_fake_get_metadata({})
    )

    assert result == ""


def test_file_outside_source_tree_returns_none() -> None:
    store = {
        "F": FileMetadata(id="F", name="stray.note", parents=("OTHER_ROOT",)),
        "OTHER_ROOT": FileMetadata(id="OTHER_ROOT", name="OtherRoot", parents=()),
    }

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result is None


def test_file_with_no_parents_returns_none() -> None:
    store = {"F": FileMetadata(id="F", name="orphan.note", parents=())}

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result is None
