"""Behavior tests for the NoteSource seam: protocol conformance and the
neutral exception taxonomy that Drive errors must satisfy when they cross it.
"""

from __future__ import annotations

import dataclasses

import pydantic
import pytest

from sn2md_worker.drive.client import (
    DriveClient,
    DrivePermanentError,
    DriveTransientError,
)
from sn2md_worker.sources import (
    ListedNote,
    NoteMetadata,
    NoteSource,
    SourceError,
    SourcePermanentError,
    SourceTransientError,
)


def _is_caught_by(error: Exception, handler: type[Exception]) -> bool:
    """Raise `error` and report whether an `except handler` clause catches it."""
    try:
        try:
            raise error
        except handler:
            return True
    except Exception:
        return False


class TestWhenDriveClientStandsInAsANoteSource:
    def test_driveclient_satisfies_the_protocol(self) -> None:
        # GIVEN a runtime-checkable, method-only protocol
        # (no DriveClient instance: constructing one needs Google credentials)

        # WHEN / THEN
        assert issubclass(DriveClient, NoteSource)


class TestWhenADriveErrorCrossesTheSeam:
    def test_permanent_error_is_caught_by_the_neutral_permanent_handler(self) -> None:
        # GIVEN
        error = DrivePermanentError("404: file gone")

        # WHEN
        caught = _is_caught_by(error, SourcePermanentError)

        # THEN
        assert caught is True

    def test_transient_error_is_caught_by_the_neutral_transient_handler(self) -> None:
        # GIVEN
        error = DriveTransientError("503 after retries")

        # WHEN
        caught = _is_caught_by(error, SourceTransientError)

        # THEN
        assert caught is True

    def test_both_errors_are_caught_by_the_neutral_base_handler(self) -> None:
        # GIVEN
        permanent = DrivePermanentError("404: file gone")
        transient = DriveTransientError("503 after retries")

        # WHEN
        permanent_caught = _is_caught_by(permanent, SourceError)
        transient_caught = _is_caught_by(transient, SourceError)

        # THEN
        assert permanent_caught is True
        assert transient_caught is True


class TestWhenCallersDistinguishRetryabilityAtTheSeam:
    def test_transient_error_is_not_caught_by_the_permanent_handler(self) -> None:
        # GIVEN
        error = DriveTransientError("503 after retries")

        # WHEN
        caught = _is_caught_by(error, SourcePermanentError)

        # THEN the retry taxonomy survives the abstraction
        assert caught is False

    def test_permanent_error_is_not_caught_by_the_transient_handler(self) -> None:
        # GIVEN
        error = DrivePermanentError("404: file gone")

        # WHEN
        caught = _is_caught_by(error, SourceTransientError)

        # THEN
        assert caught is False


class TestWhenACallerTriesToMutateASeamModel:
    def test_note_metadata_rejects_field_assignment(self) -> None:
        # GIVEN a metadata view held past the call that produced it
        meta = NoteMetadata(id="abc123", name="20260616_203930.note")

        # WHEN / THEN
        with pytest.raises(pydantic.ValidationError):
            meta.name = "renamed.note"  # type: ignore[misc]

    def test_listed_note_rejects_field_assignment(self) -> None:
        # GIVEN a listing entry held past the iterator that yielded it
        meta = NoteMetadata(id="abc123", name="20260616_203930.note")
        listed = ListedNote(metadata=meta, source_path="Notebooks/20260616_203930.note")

        # WHEN / THEN
        with pytest.raises(dataclasses.FrozenInstanceError):
            listed.source_path = "elsewhere.note"  # type: ignore[misc]
