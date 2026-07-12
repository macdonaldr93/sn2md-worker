from __future__ import annotations

import shutil
from pathlib import Path

import structlog
from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.correlation import new_correlation_id
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.sources.protocol import NoteSource
from sn2md_worker.state import conversions, page_conversions
from sn2md_worker.workflows.convert_note import convert_note
from sn2md_worker.workflows.locks import lock_for
from sn2md_worker.workflows.queues import CONVERT_QUEUE_NAME

__all__ = ["delete_output", "delete_output_impl"]

_log = get_logger("sn2md_worker.workflows.delete_output")


@DBOS.workflow()
def delete_output(file_id: str, correlation_id: str | None = None) -> None:
    with structlog.contextvars.bound_contextvars(dbos_workflow_id=DBOS.workflow_id):
        delete_output_impl(
            file_id=file_id,
            source=get_drive_client(),
            settings=get_settings(),
            correlation_id=correlation_id,
        )


def delete_output_impl(
    *,
    file_id: str,
    source: NoteSource,
    settings: Settings,
    correlation_id: str | None = None,
) -> None:
    """Mirror a Drive removal into the vault, with Supernote replace-safety.

    If a live file with the same (parent, name) already exists in Drive,
    the delete is treated as stale — we re-point `current_file_id` to the
    new file and leave the vault output untouched. Otherwise the per-note
    directory is removed and the record dropped.
    """
    # Self-healing mint: replays of pre-upgrade enqueues (and direct
    # invocations) arrive with None and still get a usable id.
    correlation_id = correlation_id or new_correlation_id()
    with structlog.contextvars.bound_contextvars(
        workflow="delete_output", file_id=file_id, correlation_id=correlation_id
    ):
        _log.info("delete_output_started")
        try:
            # First read is outside the lock only to resolve logical_key;
            # the decisive re-read happens inside the lock.
            with sql_session() as session:
                record = conversions.get_by_current_file_id(session, file_id)
            if record is None:
                _log.info("delete_output_skipped", reason="no_record")
                return

            with lock_for(record.logical_key):
                with sql_session() as session:
                    record = conversions.get_by_current_file_id(session, file_id)
                if record is None:
                    _log.info("delete_output_skipped", reason="no_record_after_lock")
                    return

                if record.parent_folder_id:
                    live = source.find_live_note(record.parent_folder_id, record.source_name)
                    if live is not None and live.id != file_id:
                        _repoint(logical_key=record.logical_key, new_file_id=live.id)
                        # Guards against a delete-side push without a
                        # matching create-side push.
                        DBOS.enqueue_workflow(
                            CONVERT_QUEUE_NAME,
                            convert_note,
                            live.id,
                            record.source_path,
                            correlation_id,
                        )
                        _log.info(
                            "delete_output_skipped",
                            reason="repointed_to_new_file",
                            new_file_id=live.id,
                            logical_key=record.logical_key,
                        )
                        return

                if not _delete_from_vault(record.output_rel_path, settings.vault.root_path):
                    _log.warning(
                        "delete_output_skipped",
                        reason="vault_delete_refused",
                        logical_key=record.logical_key,
                    )
                    return

                with sql_session() as session, session.begin():
                    conversions.delete_by_logical_key(session, record.logical_key)
                    page_conversions.delete_all_for_note(session, record.logical_key)
                _log.info(
                    "delete_output_succeeded",
                    logical_key=record.logical_key,
                    output_rel_path=record.output_rel_path,
                )
        except Exception as exc:
            _log.error("delete_output_failed", error=str(exc), exc_info=True)
            raise


def _repoint(*, logical_key: str, new_file_id: str) -> None:
    with sql_session() as session, session.begin():
        conversions.set_current_file_id(session, logical_key=logical_key, new_file_id=new_file_id)


def _delete_from_vault(output_rel_path: str, vault_root: Path) -> bool:
    normalized = output_rel_path.strip()
    if not normalized:
        _log.warning("delete_output_vault_guard", reason="empty_rel_path")
        return False

    target = (vault_root / normalized).resolve()
    root = vault_root.resolve()

    try:
        target.relative_to(root)
    except ValueError:
        _log.error(
            "delete_output_vault_guard",
            reason="path_outside_vault",
            target=str(target),
            vault_root=str(root),
        )
        return False

    if target == root:
        _log.error(
            "delete_output_vault_guard",
            reason="target_equals_vault_root",
            target=str(target),
        )
        return False

    if target.exists():
        shutil.rmtree(target)
    return True
