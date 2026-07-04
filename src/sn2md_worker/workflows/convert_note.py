from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.paths import logical_key, output_rel_path, sn2md_output_dir
from sn2md_worker.conversion.runner import run_sn2md
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import conversions
from sn2md_worker.state.conversions import ConversionUpsert
from sn2md_worker.state.models import ConversionStatus

__all__ = ["convert_note", "convert_note_impl"]

_log = get_logger("sn2md_worker.workflows.convert_note")


@DBOS.workflow()
def convert_note(file_id: str, source_path: str) -> None:
    """DBOS-durable wrapper that delegates to the plain implementation.

    Positional args because `DBOS.enqueue_workflow` is more ergonomic
    that way; the impl still uses kwargs for readability at the call site.
    """
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
    """Download → sn2md → upsert. Broken out so tests bypass DBOS."""
    key = logical_key(source_path)
    meta = drive.get_metadata(file_id)
    if meta.trashed:
        _log.info("convert_note_skip_trashed", file_id=file_id, logical_key=key)
        return

    if _already_up_to_date(key=key, file_id=file_id, md5=meta.md5_checksum):
        _log.info("convert_note_skip_up_to_date", file_id=file_id, logical_key=key)
        return

    with tempfile.TemporaryDirectory(prefix="sn2md-worker-") as tmp_root:
        note_path = drive.download(file_id, Path(tmp_root), meta.name)
        target_dir = sn2md_output_dir(source_path, settings.vault.root_path)
        target_dir.mkdir(parents=True, exist_ok=True)

        _log.info(
            "convert_note_run",
            file_id=file_id,
            logical_key=key,
            output_dir=str(target_dir),
            model=settings.sn2md.model,
        )
        run_sn2md(
            note_path=note_path,
            output_dir=target_dir,
            model=settings.sn2md.model,
            api_key=_resolve_gemini_key(settings),
        )

    _persist_success(
        key=key,
        file_id=file_id,
        parent_folder_id=meta.parents[0] if meta.parents else None,
        meta_name=meta.name,
        meta_md5=meta.md5_checksum,
        source_path=source_path,
    )
    _log.info("convert_note_success", file_id=file_id, logical_key=key)


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


def _persist_success(
    *,
    key: str,
    file_id: str,
    parent_folder_id: str | None,
    meta_name: str,
    meta_md5: str | None,
    source_path: str,
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
                last_converted_at=datetime.now(UTC),
                status=ConversionStatus.SUCCESS,
            ),
        )


def _resolve_gemini_key(settings: Settings) -> str:
    if settings.sn2md.api_key is None:
        raise RuntimeError(
            "no Gemini API key configured; set sn2md.api_key or SN2MD_WORKER__SN2MD__API_KEY"
        )
    return settings.sn2md.api_key.get_secret_value()
