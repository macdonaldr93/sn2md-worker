"""Queue-name constants for DBOS registrations.

Kept in a leaf module (no other project imports) so any workflow module
can grab a constant without pulling in the workflow that owns it — the
call sites for `CONVERT_QUEUE_NAME` fan out across `poll_changes`,
`backfill`, and `delete_output`, which would otherwise create import
cycles.
"""

from __future__ import annotations

__all__ = ["CONVERT_QUEUE_NAME", "DELETE_QUEUE_NAME", "POLL_QUEUE_NAME"]

CONVERT_QUEUE_NAME = "convert_queue"
DELETE_QUEUE_NAME = "delete_queue"
POLL_QUEUE_NAME = "poll_queue"
