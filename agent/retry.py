"""Retry with exponential backoff and full jitter (PRD 003, REL-1..REL-7)."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import anthropic

from agent.errors import TransientError

T = TypeVar("T")

# (is_transient, retry_after_seconds)
Classifier = Callable[[Exception], tuple[bool, float | None]]
OnRetry = Callable[[int, float, Exception], None]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0

    def delay(self, attempt: int, rng: random.Random, retry_after: float | None = None) -> float:
        """Full jitter: uniform(0, min(cap, base * 2^attempt)), but never less than retry-after."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**attempt))
        delay = rng.uniform(0, ceiling)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay


def classify_api_error(exc: Exception) -> tuple[bool, float | None]:
    """Decide whether an Anthropic SDK exception is worth retrying."""
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return True, None
    if isinstance(exc, anthropic.APIStatusError):
        retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
        if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
            return True, retry_after
        return False, None  # 400/401/403/404/413/422: retrying won't help
    return False, None


def classify_tool_error(exc: Exception) -> tuple[bool, float | None]:
    if isinstance(exc, TransientError):
        return True, exc.retry_after
    return False, None


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    classify: Classifier,
    on_retry: OnRetry | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> tuple[T, int]:
    """Call fn, retrying transient failures. Returns (result, attempts_used).

    Non-transient exceptions propagate immediately. If every attempt fails with a
    transient error, the last one propagates.
    """
    rng = rng or random.Random()
    for attempt in range(policy.max_attempts):
        try:
            return fn(), attempt + 1
        except Exception as exc:
            transient, retry_after = classify(exc)
            if not transient or attempt == policy.max_attempts - 1:
                raise
            delay = policy.delay(attempt, rng, retry_after)
            if on_retry:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)
    raise AssertionError("unreachable")


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form; fall back to computed backoff
