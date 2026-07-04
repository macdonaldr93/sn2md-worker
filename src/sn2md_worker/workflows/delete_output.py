from __future__ import annotations

import shutil
from pathlib import Path

from dbos import DBOS

from sn2md_worker.config import Settings, get_settings
from sn2md_worker.db import sql_session
from sn2md_worker.drive.client import DriveClient, get_drive_client
from sn2md_worker.logging import get_logger
from sn2md_worker.state import conversions

__all__ = ["delete_output", "delete_output_impl"]

_log = get_logger("sn2md_worker.workflows.delete_output")


@DBOS.workflow()
def delete_output(file_id: str) -> None:
    """DBOS-durable wrapper that delegates to the plain implementation."""
    delete_output_impl(
        file_id=file_id,
        drive=get_drive_client(),
        settings=get_settings(),
    )


def delete_output_impl(
    *,
    file_id: str,
    drive: DriveClient,
    settings: Settings,
) -> None:
    """Mirror a Drive removal into the vault, with Supernote replace-safety.

    If a live file with the same (parent, name) already exists in Drive,
    the delete is treated as stale — we re-point `current_file_id` to the
    new file and leave the vault output untouched. Otherwise the per-note
    directory is removed and the record dropped.
    """
    with sql_session() as session:
        record = conversions.get_by_current_file_id(session, file_id)

    if record is None:
        _log.info("delete_output_no_record", file_id=file_id)
        return

    if record.parent_folder_id:
        live = drive.find_live_note(record.parent_folder_id, record.source_name)
        if live is not None and live.id != file_id:
            _repoint(logical_key=record.logical_key, new_file_id=live.id)
            _log.info(
                "delete_output_repoint",
                old_file_id=file_id,
                new_file_id=live.id,
                logical_key=record.logical_key,
            )
            return

    if not _delete_from_vault(record.output_rel_path, settings.vault.root_path):
        return

    with sql_session() as session, session.begin():
        conversions.delete_by_logical_key(session, record.logical_key)
    _log.info(
        "delete_output_removed",
        file_id=file_id,
        logical_key=record.logical_key,
        output_rel_path=record.output_rel_path,
    )


def _repoint(*, logical_key: str, new_file_id: str) -> None:
    with sql_session() as session, session.begin():
        record = conversions.get_by_logical_key(session, logical_key)
        if record is not None:
            record.current_file_id = new_file_id


def _delete_from_vault(output_rel_path: str, vault_root: Path) -> bool:
    normalized = output_rel_path.strip()
    if not normalized:
        _log.warning("delete_output_skip_empty_rel_path")
        return False

    target = (vault_root / normalized).resolve()
    root = vault_root.resolve()

    try:
        target.relative_to(root)
    except ValueError:
        _log.error(
            "delete_output_path_outside_vault",
            target=str(target),
            vault_root=str(root),
        )
        return False

    if target == root:
        _log.error("delete_output_refuses_to_delete_vault_root", target=str(target))
        return False

    if target.exists():
        shutil.rmtree(target)
    return True
