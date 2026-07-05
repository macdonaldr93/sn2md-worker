from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.multi_page import (
    MultiPageResult,
    page_index_from_filename,
    run_multi_page,
)
from sn2md_worker.conversion.paths import logical_key, note_output_dir, output_rel_path
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import conversions, page_conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.state.page_conversions import PageConversionUpsert

__all__ = ["convert_note", "convert_note_impl"]

_log = get_logger("sn2md_worker.workflows.convert_note")


@DBOS.workflow()
def convert_note(file_id: str, source_path: str) -> None:
    """DBOS-durable wrapper that delegates to the plain implementation."""
    convert_note_impl(
        file_id=file_id,
        source_path=source_path,
        drive=get_drive_client(),
        settings=get_settings(),
    )


def convert_note_impl(
    *,
    file_id: str,
    source_path: str,
    drive: DriveClient,
    settings: Settings,
) -> None:
    """Download → per-page convert → upsert. Split so tests bypass DBOS."""
    key = logical_key(source_path)
    with structlog.contextvars.bound_contextvars(
        workflow="convert_note", file_id=file_id, logical_key=key
    ):
        _log.info("convert_note_started")
        try:
            meta = drive.get_metadata(file_id)
            if meta.trashed:
                _log.info("convert_note_skipped", reason="trashed")
                return

            if _already_up_to_date(key=key, file_id=file_id, md5=meta.md5_checksum):
                _log.info("convert_note_skipped", reason="up_to_date")
                return

            existing_pages = _load_existing_pages(key)
            now = datetime.now(UTC)

            output_dir = note_output_dir(source_path, settings.vault.root_path)
            with tempfile.TemporaryDirectory(prefix="sn2md-worker-") as tmp_root:
                note_path = drive.download(file_id, Path(tmp_root), meta.name)

                _log.info(
                    "convert_note_running_multi_page",
                    output_dir=str(output_dir),
                    model=settings.sn2md.model,
                    known_pages=len(existing_pages),
                )
                result = run_multi_page(
                    note_path=note_path,
                    output_dir=output_dir,
                    model=settings.sn2md.model,
                    api_key=_resolve_gemini_key(settings),
                    existing_pages=existing_pages,
                    now=now,
                )

            _persist_success(
                key=key,
                file_id=file_id,
                parent_folder_id=meta.parents[0] if meta.parents else None,
                meta_name=meta.name,
                meta_md5=meta.md5_checksum,
                source_path=source_path,
                result=result,
                now=now,
            )
            _cleanup_stale_pages(
                note_output_dir=output_dir,
                current_page_count=len(result.pages),
                note_basename=Path(meta.name).stem,
            )

            _log.info(
                "convert_note_succeeded",
                pages=len(result.pages),
                gemini_calls=result.gemini_calls,
                cache_hits=result.cache_hits,
            )
        except Exception as exc:
            _log.error("convert_note_failed", error=str(exc), exc_info=True)
            raise


def _already_up_to_date(*, key: str, file_id: str, md5: str | None) -> bool:
    with sql_session() as session:
        record = conversions.get_by_logical_key(session, key)
        if record is None:
            return False
        return (
            record.source_md5 == md5
            and record.last_status == ConversionStatus.SUCCESS
            and record.current_file_id == file_id
        )


def _load_existing_pages(logical_key_value: str) -> dict[int, str]:
    with sql_session() as session:
        rows = page_conversions.list_for_note(session, logical_key_value)
    return {row.page_index: row.page_md5 for row in rows}


def _persist_success(
    *,
    key: str,
    file_id: str,
    parent_folder_id: str | None,
    meta_name: str,
    meta_md5: str | None,
    source_path: str,
    result: MultiPageResult,
    now: datetime,
) -> None:
    with sql_session() as session, session.begin():
        conversions.upsert(
            session,
            ConversionUpsert(
                logical_key=key,
                current_file_id=file_id,
                parent_folder_id=parent_folder_id,
                source_name=meta_name,
                source_path=source_path,
                source_md5=meta_md5,
                output_rel_path=output_rel_path(source_path),
                last_converted_at=now,
                status=ConversionStatus.SUCCESS,
            ),
        )
        for page in result.pages:
            page_conversions.upsert(
                session,
                PageConversionUpsert(
                    logical_key=key,
                    page_index=page.page_index,
                    page_md5=page.page_md5,
                    output_rel_path=page.output_rel_path,
                    last_converted_at=now,
                ),
            )
        page_conversions.delete_pages_at_or_beyond(
            session, logical_key=key, page_index=len(result.pages)
        )


def _cleanup_stale_pages(
    *,
    note_output_dir: Path,
    current_page_count: int,
    note_basename: str,
) -> None:
    """Delete `page-NN.md` / `page-NN.png` for pages beyond the current
    count, plus the legacy flat `<basename>.md` and sidecar if either
    lingers from pre-multi-page conversions.
    """
    if not note_output_dir.exists():
        return

    legacy_flat = note_output_dir / f"{note_basename}.md"
    if legacy_flat.is_file():
        legacy_flat.unlink()
    legacy_sidecar = note_output_dir / ".sn2md.metadata.yaml"
    if legacy_sidecar.is_file():
        legacy_sidecar.unlink()

    for stale_path in note_output_dir.glob("page-*.md"):
        idx = page_index_from_filename(stale_path.name)
        if idx is None or idx >= current_page_count:
            stale_path.unlink(missing_ok=True)
            if idx is not None:
                (note_output_dir / f"page-{idx + 1:02d}.png").unlink(missing_ok=True)


def _resolve_gemini_key(settings: Settings) -> str:
    if settings.sn2md.api_key is not None:
        return settings.sn2md.api_key.get_secret_value()
    env_key = os.environ.get("LLM_GEMINI_KEY", "")
    if env_key:
        return env_key
    raise RuntimeError(
        "no Gemini API key configured; set LLM_GEMINI_KEY env var, "
        "sn2md.api_key in config.toml, or SN2MD_WORKER__SN2MD__API_KEY"
    )
