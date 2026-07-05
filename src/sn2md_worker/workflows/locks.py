"""Per-logical-key OS advisory locks. `convert_note` and `delete_output`
both mutate the same vault dir + DB rows for a note; serializing here
keeps delete_output from rmtree-ing under an in-flight convert."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from filelock import FileLock

__all__ = ["lock_for"]

_LOCK_DIR = Path(tempfile.gettempdir()) / "sn2md-worker-locks"


def lock_for(logical_key_value: str) -> FileLock:
    """FileLock keyed by a stable hash of logical_key; lockfiles land
    outside the vault so Obsidian never sees them."""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(logical_key_value.encode("utf-8")).hexdigest()[:16]
    return FileLock(str(_LOCK_DIR / f"{digest}.lock"))
