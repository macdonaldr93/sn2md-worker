from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["now_utc"]


def now_utc() -> datetime:
    """Return the current wall-clock time as a UTC-aware datetime.

    Single seam for production code that needs "right now" so tests can
    monkeypatch it to a fixed instant. Prefer this over calling
    `datetime.now(UTC)` directly anywhere a value is compared against a
    persisted timestamp (channel expiry, TTL headroom, readiness gate).
    """
    return datetime.now(UTC)
