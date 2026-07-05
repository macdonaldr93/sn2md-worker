from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "BootStepResult",
    "StartupStatus",
    "StepStatus",
    "get_startup_status",
    "set_startup_status",
]


StepStatus = Literal["ok", "deferred", "failed"]


@dataclass(frozen=True)
class BootStepResult:
    """Outcome of a single boot-time initialization step.

    `error` carries the message of a `failed` step (prefixed with the
    step name so aggregate `last_error` reporting is grep-friendly);
    `None` for `ok` or `deferred`.
    """

    status: StepStatus
    error: str | None


@dataclass(frozen=True)
class StartupStatus:
    """Outcome of boot-time initialization.

    `deferred` means the step was intentionally skipped because a
    prerequisite wasn't ready (typically no DriveClient — the dev-mode
    path). `failed` means the step tried and errored; `last_error`
    carries the message of the first failure so ops can grep once
    instead of tailing logs.
    """

    drive_client: StepStatus
    seed_cursor: StepStatus
    ensure_channel: StepStatus
    backfill_enqueue: StepStatus
    last_error: str | None


class _Holder:
    status: StartupStatus | None = None


def set_startup_status(status: StartupStatus) -> None:
    _Holder.status = status


def get_startup_status() -> StartupStatus | None:
    return _Holder.status
