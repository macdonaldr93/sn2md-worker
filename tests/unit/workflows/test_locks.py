from __future__ import annotations

from sn2md_worker.workflows.locks import lock_for


class TestSameKeyProducesLockfileAtSamePath:
    def test_two_locks_share_a_lockfile(self) -> None:
        # GIVEN / WHEN
        first = lock_for("Notebooks/Journal/2026-07.note")
        second = lock_for("Notebooks/Journal/2026-07.note")

        # THEN — same path → fcntl advisory lock actually serializes.
        assert first.lock_file == second.lock_file


class TestDifferentKeysProduceDifferentLockfiles:
    def test_two_keys_do_not_share_a_lockfile(self) -> None:
        # GIVEN / WHEN
        first = lock_for("Notebooks/foo.note")
        second = lock_for("Notebooks/bar.note")

        # THEN
        assert first.lock_file != second.lock_file
