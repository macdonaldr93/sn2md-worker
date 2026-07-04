from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sn2md_worker.conversion.runner import Sn2mdRunError, run_sn2md


class TestWhenSn2mdSucceeds:
    def test_removes_the_sidecar_metadata_file_from_the_output_dir(self, tmp_path: Path) -> None:
        # GIVEN — a fake sn2md that writes the standard output layout
        def fake_import(**kwargs: Any) -> None:
            output_dir = Path(kwargs["output"])
            note_dir = output_dir / "sample"
            note_dir.mkdir(parents=True, exist_ok=True)
            (note_dir / "sample.md").write_text("# sample")
            (note_dir / "image.png").write_bytes(b"png-bytes")
            (note_dir / ".sn2md.metadata.yaml").write_text("version: 1")

        # WHEN
        with patch(
            "sn2md_worker.conversion.runner.import_supernote_file_core",
            side_effect=fake_import,
        ):
            run_sn2md(
                note_path=tmp_path / "sample.note",
                output_dir=tmp_path,
                model="fake-model",
                api_key="fake-key",
            )

        # THEN
        assert (tmp_path / "sample" / "sample.md").exists()
        assert (tmp_path / "sample" / "image.png").exists()
        assert not (tmp_path / "sample" / ".sn2md.metadata.yaml").exists()

    def test_passes_the_model_and_api_key_through_to_sn2md(self, tmp_path: Path) -> None:
        # GIVEN
        captured: dict[str, Any] = {}

        def capture(**kwargs: Any) -> None:
            captured.update(kwargs)

        # WHEN
        with patch(
            "sn2md_worker.conversion.runner.import_supernote_file_core", side_effect=capture
        ):
            run_sn2md(
                note_path=tmp_path / "x.note",
                output_dir=tmp_path,
                model="gemini/gemini-2.5-pro",
                api_key="secret",
            )

        # THEN
        assert captured["file_name"] == str(tmp_path / "x.note")
        assert captured["output"] == str(tmp_path)
        assert captured["force"] is True
        assert captured["progress"] is False
        assert captured["config"].model == "gemini/gemini-2.5-pro"
        assert captured["config"].api_key == "secret"


class TestWhenSn2mdRaises:
    def test_wraps_the_underlying_exception_in_sn2md_run_error(self, tmp_path: Path) -> None:
        # GIVEN
        def blow_up(**kwargs: Any) -> None:
            raise ValueError("bad note")

        # WHEN / THEN
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
