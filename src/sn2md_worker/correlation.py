"""Correlation identity for cross-boundary tracing.

`request_id` (bound by `app.RequestIdMiddleware`) identifies one inbound
HTTP request and lives only inside that request's contextvars scope.
`correlation_id` identifies one end-to-end logical operation: minted once
at each root trigger (webhook receipt, cron tick, startup helper) and
passed as an explicit trailing workflow argument, because DBOS persists
workflow args durably while contextvars never cross onto worker threads.
"""

from __future__ import annotations

from uuid import uuid4

__all__ = ["new_correlation_id"]


def new_correlation_id() -> str:
    """Mint a fresh correlation id.

    Same 16-hex shape as `RequestIdMiddleware`'s generated request ids,
    so both id kinds stay grep-friendly in Docker logs.
    """
    return uuid4().hex[:16]
