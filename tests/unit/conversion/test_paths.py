from __future__ import annotations

from pathlib import Path

import pytest

from sn2md_worker.conversion.paths import (
    UnsafePathError,
    basename,
    logical_key,
    note_output_dir,
    output_rel_path,
    sn2md_output_dir,
)


class TestLogicalKey:
    def test_nested_path_unchanged(self) -> None:
        assert logical_key("Notebooks/Journal/2026-07.note") == "Notebooks/Journal/2026-07.note"

    def test_strips_leading_and_trailing_slashes(self) -> None:
        assert logical_key("/Notebooks/2026-07.note/") == "Notebooks/2026-07.note"

    def test_normalizes_backslashes(self) -> None:
        assert logical_key("Notebooks\\Journal\\2026-07.note") == "Notebooks/Journal/2026-07.note"

    def test_strips_whitespace(self) -> None:
        assert logical_key("  Notebooks/2026-07.note  ") == "Notebooks/2026-07.note"

    def test_root_level_note(self) -> None:
        assert logical_key("2026-07.note") == "2026-07.note"


class TestBasename:
    def test_strips_note_extension(self) -> None:
        assert basename("Notebooks/2026-07.note") == "2026-07"

    def test_root_level(self) -> None:
        assert basename("2026-07.note") == "2026-07"

    def test_multiple_dots_only_strips_final_extension(self) -> None:
        assert basename("Notebooks/2026.07.some.note") == "2026.07.some"


class TestSn2mdOutputDir:
    def test_nested_mirrors_drive_layout(self) -> None:
        assert sn2md_output_dir("Notebooks/Journal/2026-07.note", Path("/vault")) == Path(
            "/vault/Notebooks/Journal"
        )

    def test_root_level_returns_vault_root(self) -> None:
        assert sn2md_output_dir("2026-07.note", Path("/vault")) == Path("/vault")

    def test_normalizes_backslashes_before_mapping(self) -> None:
        assert sn2md_output_dir("Notebooks\\Journal\\2026-07.note", Path("/vault")) == Path(
            "/vault/Notebooks/Journal"
        )

    def test_relative_vault_root(self) -> None:
        assert sn2md_output_dir("Notebooks/Journal/2026-07.note", Path("./vault")) == Path(
            "vault/Notebooks/Journal"
        )


class TestNoteOutputDir:
    def test_nested_gives_per_note_folder_under_vault(self) -> None:
        assert note_output_dir("Notebooks/Journal/2026-07.note", Path("/vault")) == Path(
            "/vault/Notebooks/Journal/2026-07"
        )

    def test_root_level_gives_folder_next_to_vault_root(self) -> None:
        assert note_output_dir("2026-07.note", Path("/vault")) == Path("/vault/2026-07")


class TestOutputRelPath:
    def test_nested_includes_basename_subdir(self) -> None:
        assert output_rel_path("Notebooks/Journal/2026-07.note") == "Notebooks/Journal/2026-07"

    def test_root_level_is_just_basename(self) -> None:
        assert output_rel_path("2026-07.note") == "2026-07"


@pytest.mark.parametrize(
    ("source", "vault", "expected_sn2md_dir", "expected_rel"),
    [
        ("Notebooks/2026-07.note", "/vault", "/vault/Notebooks", "Notebooks/2026-07"),
        ("2026-07.note", "/vault", "/vault", "2026-07"),
        ("A/B/C/D.note", "/vault", "/vault/A/B/C", "A/B/C/D"),
    ],
)
def test_paths_stay_consistent_with_each_other(
    source: str, vault: str, expected_sn2md_dir: str, expected_rel: str
) -> None:
    assert sn2md_output_dir(source, Path(vault)) == Path(expected_sn2md_dir)
    assert output_rel_path(source) == expected_rel
    assert str(Path(vault) / expected_rel) == f"{expected_sn2md_dir}/{basename(source)}"


@pytest.mark.parametrize(
    "source",
    [
        "../escape.note",
        "Notebooks/../../etc/passwd.note",
        "..",
        "./inside.note",
        "Notebooks/./sneaky.note",
        "with\x00null.note",
        "Notebooks/with\x00null.note",
        "Notebooks//double-slash.note",
        "",
        "   ",
        "/",
    ],
)
def test_unsafe_paths_are_rejected(source: str) -> None:
    with pytest.raises(UnsafePathError):
        logical_key(source)


@pytest.mark.parametrize(
    "source",
    [
        "CON.note",
        "prn.note",  # case-insensitive
        "Aux.note",
        "NUL.note",
        "COM1.note",
        "COM9.note",
        "LPT1.note",
        "Notebooks/CON.note",
    ],
)
def test_windows_reserved_names_are_rejected(source: str) -> None:
    with pytest.raises(UnsafePathError, match="Windows-reserved"):
        logical_key(source)


@pytest.mark.parametrize(
    "source",
    [
        "com10.note",  # only COM1..COM9 are reserved
        "concord.note",  # starts with CON but stem is CONCORD
        "prn-notes/journal.note",
    ],
)
def test_names_that_look_reserved_but_are_not_are_accepted(source: str) -> None:
    # Should not raise
    logical_key(source)
