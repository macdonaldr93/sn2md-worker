"""Queue-name constants for DBOS registrations.

Kept in a leaf module (no other project imports) so any workflow module
can grab a constant without pulling in the workflow that owns it — the
call sites for `CONVERT_QUEUE_NAME` fan out across `poll_changes`,
`backfill`, and `delete_output`, which would otherwise create import
cycles.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dbos import SetEnqueueOptions
from dbos._context import _dbos_context_var

__all__ = [
    "CONVERT_QUEUE_NAME",
    "DELETE_QUEUE_NAME",
    "POLL_QUEUE_NAME",
    "detached_enqueue",
]

CONVERT_QUEUE_NAME = "convert_queue"
DELETE_QUEUE_NAME = "delete_queue"
POLL_QUEUE_NAME = "poll_queue"


@contextmanager
def detached_enqueue(*, deduplication_id: str | None = None) -> Iterator[None]:
    """Run `DBOS.enqueue_workflow` outside any enclosing workflow's step context.

    When `DBOS.enqueue_workflow` runs inside a `@DBOS.workflow()`, DBOS records
    the enqueue in `operation_outputs` keyed on `(parent_workflow_id, function_id)`.
    Because our workflows (`poll_changes`, `backfill`, `delete_output`) read live
    external state and are non-deterministic across recovery attempts, positional
    step replay corrupts or silently drops child enqueues on recovery. Temporarily
    clearing the active workflow context makes DBOS enqueue a top-level workflow
    governed solely by `SetEnqueueOptions(deduplication_id=...)` and our SQLite
    state tables.
    """
    token = _dbos_context_var.set(None)
    try:
        if deduplication_id is not None:
            with SetEnqueueOptions(deduplication_id=deduplication_id):
                yield
        else:
            yield
    finally:
        _dbos_context_var.reset(token)
