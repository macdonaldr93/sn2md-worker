from __future__ import annotations

from sn2md_worker.drive.paths import resolve_source_path
from sn2md_worker.sources.models import NoteMetadata

ROOT = "ROOT_ID"


def _fake_get_metadata(store: dict[str, NoteMetadata]):
    def _lookup(file_id: str) -> NoteMetadata:
        return store[file_id]

    return _lookup


def test_file_directly_in_root() -> None:
    store = {"F": NoteMetadata(id="F", name="2026-07.note", parents=(ROOT,))}

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result == "2026-07.note"


def test_file_in_nested_subfolders() -> None:
    store = {
        "F": NoteMetadata(id="F", name="2026-07.note", parents=("SUB",)),
        "SUB": NoteMetadata(id="SUB", name="Journal", parents=("PARENT",)),
        "PARENT": NoteMetadata(id="PARENT", name="Notebooks", parents=(ROOT,)),
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
        "F": NoteMetadata(id="F", name="stray.note", parents=("OTHER_ROOT",)),
        "OTHER_ROOT": NoteMetadata(id="OTHER_ROOT", name="OtherRoot", parents=()),
    }

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result is None


def test_file_with_no_parents_returns_none() -> None:
    store = {"F": NoteMetadata(id="F", name="orphan.note", parents=())}

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result is None


def test_multi_parent_falls_back_to_a_parent_that_reaches_the_root() -> None:
    # parents[0] leads outside our source tree; parents[1] leads to root.
    # Old impl silently picked parents[0] and returned None; new impl
    # tries each parent and returns the path via the one that resolves.
    store = {
        "F": NoteMetadata(id="F", name="2026-07.note", parents=("STRAY", "SUB")),
        "STRAY": NoteMetadata(id="STRAY", name="Strays", parents=()),
        "SUB": NoteMetadata(id="SUB", name="Journal", parents=(ROOT,)),
    }

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result == "Journal/2026-07.note"


def test_no_parent_reaches_root_returns_none() -> None:
    store = {
        "F": NoteMetadata(id="F", name="stray.note", parents=("A", "B")),
        "A": NoteMetadata(id="A", name="A", parents=()),
        "B": NoteMetadata(id="B", name="B", parents=("A",)),
    }

    result = resolve_source_path(
        file_id="F", root_folder_id=ROOT, get_metadata=_fake_get_metadata(store)
    )

    assert result is None
