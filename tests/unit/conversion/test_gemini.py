from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tenacity import RetryCallState, wait_none

from sn2md_worker.conversion import gemini
from sn2md_worker.conversion.gemini import transcribe_page


@pytest.fixture
def instant_gemini_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-out tenacity's backoff sleeps so retry tests run fast."""
    monkeypatch.setattr(gemini._call_gemini_with_retry.retry, "wait", wait_none())


class ResourceExhausted(Exception):
    """Stand-in for the google SDK's throttle error type. Only the class
    NAME matters to `_is_throttle_error`; the classifier never imports
    the real type."""


def _make_failed_retry_state(exc: BaseException) -> RetryCallState:
    """Build a RetryCallState carrying `exc` as its recorded failure, the
    shape tenacity hands to stop/wait policies between attempts."""
    state = RetryCallState(retry_object=None, fn=None, args=(), kwargs={})  # type: ignore[arg-type]
    state.set_exception((type(exc), exc, None))
    return state


def _transcribe_one_page() -> str:
    """Call transcribe_page with fixed fake arguments; the SDK call is
    expected to be patched at the gemini module boundary."""
    return transcribe_page(
        png_path=Path("page-01.png"),
        context="previous page tail",
        api_key="fake-key",
        model="fake-model",
        prompt_template="prompt {context}",
    )


class TestIsThrottleError:
    def test_message_containing_429_is_throttle(self) -> None:
        assert gemini._is_throttle_error(RuntimeError("HTTP error 429 from Gemini")) is True

    def test_each_marker_matches_case_insensitively(self) -> None:
        for message in (
            "Rate Limit exceeded",
            "RATELIMIT hit for model",
            "code RESOURCE_EXHAUSTED",
            "ResourceExhausted while calling model",
            "Quota exceeded for quota metric",
            "Too Many Requests",
        ):
            assert gemini._is_throttle_error(RuntimeError(message)) is True, message

    def test_type_name_alone_matches_without_a_message(self) -> None:
        assert gemini._is_throttle_error(ResourceExhausted()) is True

    def test_chained_cause_is_walked(self) -> None:
        outer = RuntimeError("sn2md wrapper: model call failed")
        outer.__cause__ = ResourceExhausted("upstream said slow down")
        assert gemini._is_throttle_error(outer) is True

    def test_context_is_walked_when_cause_is_absent(self) -> None:
        outer = RuntimeError("sn2md wrapper: model call failed")
        outer.__context__ = RuntimeError("got 429 from upstream")
        assert gemini._is_throttle_error(outer) is True

    def test_cyclic_chain_terminates_and_returns_false(self) -> None:
        first = RuntimeError("first wrapper")
        second = RuntimeError("second wrapper")
        first.__cause__ = second
        second.__cause__ = first
        assert gemini._is_throttle_error(first) is False

    def test_generic_error_is_not_throttle(self) -> None:
        assert gemini._is_throttle_error(ConnectionError("gemini down")) is False


class TestGeminiWaitPolicy:
    def test_throttled_failure_uses_the_longer_backoff_curve(self) -> None:
        # GIVEN: a first-attempt failure that looks like a 429.
        state = _make_failed_retry_state(RuntimeError("429 Too Many Requests"))

        # WHEN
        wait_seconds = gemini._gemini_wait(state)

        # THEN: the wait starts at the throttle curve's floor; the fast
        # curve's first attempt tops out below it even with jitter.
        assert wait_seconds >= gemini._GEMINI_THROTTLE_BACKOFF_INITIAL_SECONDS

    def test_generic_failure_keeps_the_fast_backoff_curve(self) -> None:
        # GIVEN
        state = _make_failed_retry_state(ConnectionError("gemini down"))

        # WHEN
        wait_seconds = gemini._gemini_wait(state)

        # THEN
        assert wait_seconds < gemini._GEMINI_THROTTLE_BACKOFF_INITIAL_SECONDS


class TestWhenGeminiSucceedsFirstTry:
    def test_passes_the_arguments_through_to_the_sdk_call_once(self) -> None:
        # GIVEN
        with patch(
            "sn2md_worker.conversion.gemini.image_to_markdown",
            return_value="markdown body",
        ) as fake_llm:
            # WHEN
            body = _transcribe_one_page()

        # THEN: one call, positional args in sn2md's expected order.
        assert body == "markdown body"
        assert fake_llm.call_count == 1
        assert fake_llm.call_args.args == (
            "page-01.png",
            "previous page tail",
            "fake-key",
            "fake-model",
            "prompt {context}",
        )


class TestWhenGeminiThrottles:
    def test_a_429_message_outlives_the_fast_budget_and_succeeds_on_attempt_five(
        self,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN: Gemini throttles for four attempts, then the per-minute
        # quota window ends. The old 3-attempt cap would have failed on
        # attempt 3; the throttle budget rides it out.
        attempt_counter = {"n": 0}

        def throttled_then_ok(*_args: object, **_kwargs: object) -> str:
            attempt_counter["n"] += 1
            if attempt_counter["n"] < 5:
                raise RuntimeError("429 Too Many Requests: per-minute quota exceeded")
            return "markdown after the throttle window"

        with patch(
            "sn2md_worker.conversion.gemini.image_to_markdown",
            side_effect=throttled_then_ok,
        ) as fake_llm:
            # WHEN
            body = _transcribe_one_page()

        # THEN: five attempts, then the successful transcription lands.
        assert fake_llm.call_count == 5
        assert body == "markdown after the throttle window"

    def test_a_chained_resource_exhausted_cause_burns_the_full_extended_budget(
        self,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN: sn2md-style wrapping, the throttle signal lives on
        # __cause__ (a type NAMED ResourceExhausted), not in the outer
        # message.
        def always_throttled(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("sn2md wrapper: model call failed") from ResourceExhausted(
                "upstream said slow down"
            )

        with (
            patch(
                "sn2md_worker.conversion.gemini.image_to_markdown",
                side_effect=always_throttled,
            ) as fake_llm,
            pytest.raises(RuntimeError, match="model call failed"),
        ):
            # WHEN
            _transcribe_one_page()

        # THEN: all eight throttle-budget attempts were spent, then the
        # final error was reraised for the caller to wrap.
        assert fake_llm.call_count == 8


class TestWhenGeminiFailsForOtherReasons:
    def test_a_generic_error_still_stops_after_the_fast_three_attempt_budget(
        self,
        instant_gemini_retries: None,  # noqa: ARG002
    ) -> None:
        # GIVEN: nothing 429-shaped about this failure, so the classifier
        # fails closed and keeps the fast budget.
        with (
            patch(
                "sn2md_worker.conversion.gemini.image_to_markdown",
                side_effect=ValueError("malformed response payload"),
            ) as fake_llm,
            pytest.raises(ValueError, match="malformed response payload"),
        ):
            # WHEN
            _transcribe_one_page()

        # THEN: three attempts (initial + 2 retries), then reraised.
        assert fake_llm.call_count == 3
