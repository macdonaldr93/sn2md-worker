from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.conversion.multi_page import (
    MultiPageResult,
    PageOutcome,
    page_index_from_filename,
    run_multi_page,
)
from sn2md_worker.conversion.paths import (
    UnsafePathError,
    logical_key,
    note_output_dir,
    output_rel_path,
    resolve_vault_subdir,
)
from sn2md_worker.correlation import new_correlation_id
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.sources.models import NoteMetadata
from sn2md_worker.sources.protocol import (
    NoteSource,
    SourceNotFoundError,
    SourcePermanentError,
)
from sn2md_worker.state import conversions, page_conversions
from sn2md_worker.state.conversions import ConversionRecordView, ConversionUpsert
from sn2md_worker.state.models import ConversionStatus
from sn2md_worker.state.page_conversions import PageConversionUpsert
from sn2md_worker.workflows.locks import lock_for

__all__ = ["convert_note", "convert_note_impl"]

_log = get_logger("sn2md_worker.workflows.convert_note")


@DBOS.workflow()
def convert_note(file_id: str, source_path: str, correlation_id: str | None = None) -> None:
    with structlog.contextvars.bound_contextvars(dbos_workflow_id=DBOS.workflow_id):
        convert_note_impl(
            file_id=file_id,
            source_path=source_path,
            source=get_drive_client(),
            settings=get_settings(),
            correlation_id=correlation_id,
        )


def convert_note_impl(
    *,
    file_id: str,
    source_path: str,
    source: NoteSource,
    settings: Settings,
    correlation_id: str | None = None,
) -> None:
    """Download → per-page convert → upsert. Split so tests bypass DBOS."""
    # Self-healing mint: replays of pre-upgrade enqueues (and direct
    # invocations) arrive with None and still get a usable id.
    correlation_id = correlation_id or new_correlation_id()
    try:
        key = logical_key(source_path)
    except UnsafePathError as exc:
        # DBOS would retry forever on a permanently-unsafe path.
        _log.warning(
            "convert_note_skipped",
            reason="unsafe_path",
            source_path=source_path,
            file_id=file_id,
            correlation_id=correlation_id,
            error=str(exc),
        )
        return
    with structlog.contextvars.bound_contextvars(
        workflow="convert_note", file_id=file_id, logical_key=key, correlation_id=correlation_id
    ):
        _log.info("convert_note_started")
        # Lock before the up-to-date check so two workers can't both
        # observe "stale" and both re-run.
        stale_records: list[ConversionRecordView] = []
        with lock_for(key):
            meta: NoteMetadata | None = None
            try:
                meta = source.get_metadata(file_id)
                if meta.trashed:
                    _log.info("convert_note_skipped", reason="trashed")
                    return

                if _already_up_to_date(key=key, file_id=file_id, md5=meta.md5_checksum):
                    _log.info("convert_note_skipped", reason="up_to_date")
                    return

                stale_records = _load_stale_records_for_file(file_id=file_id, exclude_key=key)
                existing_pages = _load_existing_pages(key)
                now = datetime.now(UTC)
                parent_folder_id = meta.parents[0] if meta.parents else None
                output_dir = note_output_dir(source_path, settings.vault.root_path)
                if not existing_pages and stale_records:
                    existing_pages = _seed_from_prior_location(
                        stale=stale_records[0],
                        output_dir=output_dir,
                        vault_root=settings.vault.root_path,
                    )

                with tempfile.TemporaryDirectory(prefix="sn2md-worker-") as tmp_root:
                    note_path = source.download(file_id, Path(tmp_root), meta.name)

                    # PENDING before the loop → crash-visible to delete_output
                    # and refused as up-to-date by a retry.
                    _persist_pending_start(
                        key=key,
                        file_id=file_id,
                        parent_folder_id=parent_folder_id,
                        meta_name=meta.name,
                        source_path=source_path,
                        now=now,
                    )

                    _log.info(
                        "convert_note_running_multi_page",
                        output_dir=str(output_dir),
                        model=settings.sn2md.model,
                        # DB-known rows; runner also picks up orphan .md/.png on disk.
                        db_known_pages=len(existing_pages),
                    )
                    result = run_multi_page(
                        note_path=note_path,
                        output_dir=output_dir,
                        model=settings.sn2md.model,
                        api_key=_resolve_gemini_key(settings),
                        existing_pages=existing_pages,
                        now=now,
                        prompt=settings.sn2md.prompt,
                        on_page_done=lambda page: _persist_page(key=key, page=page, when=now),
                    )

                _persist_finalize(
                    key=key,
                    result=result,
                    now=now,
                    source_md5=meta.md5_checksum,
                    output_dir=output_dir,
                    note_basename=Path(meta.name).stem,
                )

                _log.info(
                    "convert_note_succeeded",
                    pages=len(result.pages),
                    gemini_calls=result.gemini_calls,
                    cache_hits=result.cache_hits,
                )
            except SourceNotFoundError as exc:
                _log.warning(
                    "convert_note_skipped",
                    reason="source_not_found",
                    error=str(exc),
                )
                return
            except SourcePermanentError as exc:
                _persist_failure(
                    key=key,
                    file_id=file_id,
                    source_name=meta.name if meta is not None else PurePosixPath(key).name,
                    source_path=source_path,
                    error=str(exc),
                    when=datetime.now(UTC),
                )
                _log.error(
                    "convert_note_failed",
                    reason="source_permanent_error",
                    error=str(exc),
                )
                return
            except Exception as exc:
                _persist_failure(
                    key=key,
                    file_id=file_id,
                    source_name=meta.name if meta is not None else PurePosixPath(key).name,
                    source_path=source_path,
                    error=str(exc),
                    when=datetime.now(UTC),
                )
                _log.error("convert_note_failed", error=str(exc), exc_info=True)
                raise

        for stale in stale_records:
            _cleanup_moved_record(
                stale_key=stale.logical_key,
                file_id=file_id,
                vault_root=settings.vault.root_path,
            )


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


def _load_stale_records_for_file(*, file_id: str, exclude_key: str) -> list[ConversionRecordView]:
    with sql_session() as session:
        return [
            record
            for record in conversions.list_by_current_file_id(session, file_id)
            if record.logical_key != exclude_key
        ]


def _seed_from_prior_location(
    *, stale: ConversionRecordView, output_dir: Path, vault_root: Path
) -> dict[int, str]:
    prior_pages = _load_existing_pages(stale.logical_key)
    old_dir, _ = resolve_vault_subdir(stale.output_rel_path, vault_root)
    if old_dir is not None and old_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)
        for src_file in old_dir.glob("page-*.*"):
            dst_file = output_dir / src_file.name
            if not dst_file.exists():
                shutil.copy2(src_file, dst_file)
        old_index = old_dir / "index.md"
        dst_index = output_dir / "index.md"
        if old_index.is_file() and not dst_index.exists():
            shutil.copy2(old_index, dst_index)
    return prior_pages


def _persist_pending_start(
    *,
    key: str,
    file_id: str,
    parent_folder_id: str | None,
    meta_name: str,
    source_path: str,
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
                source_md5=None,
                output_rel_path=output_rel_path(source_path),
                last_converted_at=now,
                status=ConversionStatus.PENDING,
            ),
        )


def _persist_failure(
    *,
    key: str,
    file_id: str,
    source_name: str,
    source_path: str,
    error: str,
    when: datetime,
) -> None:
    with sql_session() as session, session.begin():
        conversions.record_failure(
            session,
            logical_key=key,
            current_file_id=file_id,
            source_name=source_name,
            source_path=source_path,
            error=error,
            when=when,
        )


def _persist_page(*, key: str, page: PageOutcome, when: datetime) -> None:
    with sql_session() as session, session.begin():
        page_conversions.upsert(
            session,
            PageConversionUpsert(
                logical_key=key,
                page_index=page.page_index,
                page_md5=page.page_md5,
                output_rel_path=page.output_rel_path,
                last_converted_at=when,
            ),
        )


def _persist_finalize(
    *,
    key: str,
    result: MultiPageResult,
    now: datetime,
    source_md5: str | None,
    output_dir: Path,
    note_basename: str,
) -> None:
    with sql_session() as session, session.begin():
        # UPDATE (not upsert) so `attempts` from PENDING isn't double-counted.
        conversions.mark_success(session, logical_key=key, when=now, source_md5=source_md5)
        page_conversions.delete_pages_at_or_beyond(
            session, logical_key=key, page_index=len(result.pages)
        )
    _cleanup_stale_pages(
        note_output_dir=output_dir,
        current_page_count=len(result.pages),
        note_basename=note_basename,
    )


def _cleanup_moved_record(*, stale_key: str, file_id: str, vault_root: Path) -> None:
    with lock_for(stale_key):
        with sql_session() as session:
            stale = conversions.get_by_logical_key(session, stale_key)
        if stale is None or stale.current_file_id != file_id:
            return
        if not _delete_stale_vault_dir(stale.output_rel_path, vault_root):
            return
        with sql_session() as session, session.begin():
            current = conversions.get_by_logical_key(session, stale_key)
            if current is not None and current.current_file_id == file_id:
                conversions.delete_by_logical_key(session, stale_key)
                page_conversions.delete_all_for_note(session, stale_key)


def _delete_stale_vault_dir(output_rel_path_value: str, vault_root: Path) -> bool:
    target, err = resolve_vault_subdir(output_rel_path_value, vault_root)
    if target is None:
        _log.warning(
            "convert_note_stale_vault_guard",
            reason=err,
            output_rel_path=output_rel_path_value,
            vault_root=str(vault_root),
        )
        return False
    if target.exists():
        shutil.rmtree(target)
    return True


def _cleanup_stale_pages(
    *,
    note_output_dir: Path,
    current_page_count: int,
    note_basename: str,
) -> None:
    """Prune page files beyond `current_page_count` plus legacy pre-multi-page artifacts."""
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

    # Second pass catches PNG-only orphans (crash between .png and .md write).
    for stale_png in note_output_dir.glob("page-*.png"):
        idx = page_index_from_filename(stale_png.name)
        if idx is None or idx >= current_page_count:
            stale_png.unlink(missing_ok=True)


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
