from __future__ import annotations

from sn2md_worker.correlation import new_correlation_id


def test_new_correlation_id_is_16_lowercase_hex_chars() -> None:
    # Pinned on purpose: the shape matches RequestIdMiddleware's generated
    # request ids so both id kinds stay grep-friendly in Docker logs.
    correlation_id = new_correlation_id()
    assert len(correlation_id) == 16
    assert all(c in "0123456789abcdef" for c in correlation_id)


def test_consecutive_ids_are_distinct() -> None:
    assert new_correlation_id() != new_correlation_id()
