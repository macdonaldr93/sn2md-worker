from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sn2md_worker.conversion.runner import Sn2mdRunError, run_sn2md


def test_run_sn2md_removes_sidecar(tmp_path: Path) -> None:
    def fake_import(**kwargs: Any) -> None:
        output_dir = Path(kwargs["output"])
        note_dir = output_dir / "sample"
        note_dir.mkdir(parents=True, exist_ok=True)
        (note_dir / "sample.md").write_text("# sample")
        (note_dir / "image.png").write_bytes(b"png-bytes")
        (note_dir / ".sn2md.metadata.yaml").write_text("version: 1")

    with patch(
        "sn2md_worker.conversion.runner.import_supernote_file_core", side_effect=fake_import
    ):
        run_sn2md(
            note_path=tmp_path / "sample.note",
            output_dir=tmp_path,
            model="fake-model",
            api_key="fake-key",
        )

    assert (tmp_path / "sample" / "sample.md").exists()
    assert (tmp_path / "sample" / "image.png").exists()
    assert not (tmp_path / "sample" / ".sn2md.metadata.yaml").exists()


def test_run_sn2md_passes_config_to_sn2md(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("sn2md_worker.conversion.runner.import_supernote_file_core", side_effect=capture):
        run_sn2md(
            note_path=tmp_path / "x.note",
            output_dir=tmp_path,
            model="gemini/gemini-2.5-pro",
            api_key="secret",
        )

    assert captured["file_name"] == str(tmp_path / "x.note")
    assert captured["output"] == str(tmp_path)
    assert captured["force"] is True
    assert captured["progress"] is False
    assert captured["config"].model == "gemini/gemini-2.5-pro"
    assert captured["config"].api_key == "secret"


def test_run_sn2md_wraps_underlying_error(tmp_path: Path) -> None:
    def blow_up(**kwargs: Any) -> None:
        raise ValueError("bad note")

    with (
        patch(
            "sn2md_worker.conversion.runner.import_supernote_file_core",
            side_effect=blow_up,
        ),
        pytest.raises(Sn2mdRunError, match="sn2md failed on x.note"),
    ):
        run_sn2md(
            note_path=tmp_path / "x.note",
            output_dir=tmp_path,
            model="m",
            api_key="k",
        )
