from __future__ import annotations

from pathlib import Path

from sn2md.importer import import_supernote_file_core
from sn2md.importers.note import NotebookExtractor
from sn2md.types import Config as Sn2mdConfig

__all__ = ["Sn2mdRunError", "run_sn2md"]

_SIDECAR_NAME = ".sn2md.metadata.yaml"


class Sn2mdRunError(RuntimeError):
    """Raised when sn2md fails to convert a .note file."""


def run_sn2md(
    *,
    note_path: Path,
    output_dir: Path,
    model: str,
    api_key: str,
) -> None:
    """Run sn2md against `note_path`, writing results under `output_dir`.

    sn2md's output layout is `<output_dir>/<basename>/<basename>.md` plus
    assets. We remove the `.sn2md.metadata.yaml` sidecar afterwards — we
    keep our own idempotency records and the sidecar just clutters the
    vault.
    """
    cfg = Sn2mdConfig(model=model, api_key=api_key)
    try:
        import_supernote_file_core(
            image_extractor=NotebookExtractor(),
            file_name=str(note_path),
            output=str(output_dir),
            config=cfg,
            force=True,
            progress=False,
            model=None,
        )
    except Exception as exc:
        raise Sn2mdRunError(f"sn2md failed on {note_path.name}: {exc}") from exc

    _remove_sidecars(output_dir)


def _remove_sidecars(output_dir: Path) -> None:
    for sidecar in output_dir.rglob(_SIDECAR_NAME):
        sidecar.unlink(missing_ok=True)
