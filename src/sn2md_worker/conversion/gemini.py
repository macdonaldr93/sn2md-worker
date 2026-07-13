"""One-page Gemini transcription with throttle-aware bounded retry.

Converts one rendered page PNG to a markdown body via Gemini (through
sn2md's `image_to_markdown`). Retries transiently with two budgets: a
fast one for generic failures and an extended one for 429-shaped
throttle errors, detected by marker-matching the exception chain
because the wrapped SDK errors have no stable type surface. Raises the
final error on exhaustion for the caller to wrap.
"""

from __future__ import annotations

from pathlib import Path

from sn2md.ai_utils import image_to_markdown
from tenacity import (
    RetryCallState,
    retry,
    wait_exponential_jitter,
)

from sn2md_worker.logging import get_logger

__all__ = ["transcribe_page"]

_log = get_logger("sn2md_worker.conversion.gemini")


def transcribe_page(
    *,
    png_path: Path,
    context: str,
    api_key: str,
    model: str,
    prompt_template: str,
) -> str:
    """Transcribe one rendered page PNG into a markdown body via Gemini,
    with bounded, throttle-aware exponential-backoff retry (budgets
    documented on the constants block below). Raises the final
    exception once the matching budget is exhausted.
    """
    return _call_gemini_with_retry(
        png_path=png_path,
        context=context,
        api_key=api_key,
        model=model,
        prompt_template=prompt_template,
    )


# Two retry budgets, selected per attempt by classifying the raised
# exception (see _is_throttle_error):
#
# - Fast budget (the default): 3 attempts, exponential jitter between
#   2s and 20s, bounded well under a minute. Covers transient 5xx and
#   network blips without stalling a page on a permanent 400-class
#   failure.
# - Throttle budget: 8 attempts, exponential jitter between 5s and
#   120s, roughly six to seven minutes of cumulative sleep per page in
#   the worst case. A Gemini 429 signals a per-minute quota window;
#   riding it out in place beats failing the DBOS workflow and leaving
#   the note stale until the next restart or edit. The long sleeps are
#   acceptable for this single-user service: the convert queue runs at
#   concurrency 2 and deletes run on their own queue, so a page waiting
#   out a throttle window blocks very little.
#
# Detection is string-based (each exception's type name plus str(exc),
# matched case-insensitively against _THROTTLE_MARKERS across the
# exception chain) because sn2md wraps the underlying llm-gemini /
# google SDK errors with no stable public type surface, so marker
# matching on the chain is the only stable detection. Unknown errors
# fail closed onto the fast budget.
_GEMINI_MAX_ATTEMPTS = 3
_GEMINI_BACKOFF_INITIAL_SECONDS = 2
_GEMINI_BACKOFF_MAX_SECONDS = 20
_GEMINI_THROTTLE_MAX_ATTEMPTS = 8
_GEMINI_THROTTLE_BACKOFF_INITIAL_SECONDS = 5
_GEMINI_THROTTLE_BACKOFF_MAX_SECONDS = 120

_THROTTLE_MARKERS = (
    "429",
    "rate limit",
    "ratelimit",
    "resource_exhausted",
    "resourceexhausted",
    "quota",
    "too many requests",
)
# Exception chains from wrapped SDK errors run two or three links deep;
# the bound guards against pathological or self-referencing chains.
_THROTTLE_CHAIN_MAX_DEPTH = 10


def _is_throttle_error(exc: BaseException) -> bool:
    """True when the exception, or anything in its chain, looks like a
    Gemini throttle (429 / rate limit / quota exhaustion).

    Walks `__cause__` first, falling back to `__context__`, because
    sn2md wraps the SDK errors and the throttle signal often lives one
    or two links down. Bounded depth and an id-based seen set keep the
    walk cycle-safe. Fails closed: anything unrecognized returns False
    and keeps the fast retry budget.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_THROTTLE_CHAIN_MAX_DEPTH):
        if current is None or id(current) in seen:
            return False
        seen.add(id(current))
        haystack = f"{type(current).__name__} {current}".lower()
        if any(marker in haystack for marker in _THROTTLE_MARKERS):
            return True
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return False


def _outcome_is_throttled(retry_state: RetryCallState) -> bool:
    """Classify the failure recorded on a tenacity retry state."""
    if retry_state.outcome is None:
        return False
    exc = retry_state.outcome.exception()
    return exc is not None and _is_throttle_error(exc)


def _gemini_stop(retry_state: RetryCallState) -> bool:
    """tenacity `stop` policy: throttled failures earn the extended budget."""
    max_attempts = (
        _GEMINI_THROTTLE_MAX_ATTEMPTS
        if _outcome_is_throttled(retry_state)
        else _GEMINI_MAX_ATTEMPTS
    )
    return retry_state.attempt_number >= max_attempts


_GEMINI_FAST_WAIT = wait_exponential_jitter(
    initial=_GEMINI_BACKOFF_INITIAL_SECONDS, max=_GEMINI_BACKOFF_MAX_SECONDS
)
_GEMINI_THROTTLE_WAIT = wait_exponential_jitter(
    initial=_GEMINI_THROTTLE_BACKOFF_INITIAL_SECONDS, max=_GEMINI_THROTTLE_BACKOFF_MAX_SECONDS
)


def _gemini_wait(retry_state: RetryCallState) -> float:
    """tenacity `wait` policy: pick the backoff curve matching the budget."""
    backoff = _GEMINI_THROTTLE_WAIT if _outcome_is_throttled(retry_state) else _GEMINI_FAST_WAIT
    return backoff(retry_state)


def _log_gemini_retry(retry_state: RetryCallState) -> None:
    """tenacity `before_sleep` hook — one structured warning per backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
    next_wait = retry_state.next_action.sleep if retry_state.next_action is not None else 0
    throttled = exc is not None and _is_throttle_error(exc)
    _log.warning(
        "gemini_call_retry_scheduled",
        attempt=retry_state.attempt_number,
        max_attempts=_GEMINI_THROTTLE_MAX_ATTEMPTS if throttled else _GEMINI_MAX_ATTEMPTS,
        next_wait_seconds=round(next_wait, 2),
        error_type=type(exc).__name__ if exc is not None else None,
        error=str(exc) if exc is not None else None,
        throttled=throttled,
    )


@retry(
    stop=_gemini_stop,
    wait=_gemini_wait,
    before_sleep=_log_gemini_retry,
    reraise=True,
)
def _call_gemini_with_retry(
    *,
    png_path: Path,
    context: str,
    api_key: str,
    model: str,
    prompt_template: str,
) -> str:
    """Invoke sn2md's Gemini call under the tenacity retry policy.

    Idempotent by construction — no side effects until the caller writes
    the returned markdown to disk — so retrying is always safe.
    """
    result: str = image_to_markdown(str(png_path), context, api_key, model, prompt_template)
    return result
