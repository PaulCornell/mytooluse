import random

import anthropic
import httpx2
import pytest

from agent.errors import ToolExecutionError, TransientToolError
from agent.retry import RetryPolicy, call_with_retry, classify_api_error, classify_tool_error


def api_error(cls, status: int, headers: dict | None = None):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, headers=headers or {})
    return cls(f"HTTP {status}", response=response, body=None)


def test_retries_transient_then_succeeds():
    calls, sleeps, retries = [], [], []

    def fn():
        calls.append(1)
        if len(calls) < 4:
            raise api_error(anthropic.RateLimitError, 429)
        return "ok"

    result, attempts = call_with_retry(
        fn, policy=RetryPolicy(max_attempts=4), classify=classify_api_error,
        on_retry=lambda n, d, e: retries.append(n), sleep=sleeps.append, rng=random.Random(0),
    )
    assert (result, attempts) == ("ok", 4)
    assert retries == [1, 2, 3] and len(sleeps) == 3


def test_does_not_retry_bad_request():
    sleeps = []

    def fn():
        raise api_error(anthropic.BadRequestError, 400)

    with pytest.raises(anthropic.BadRequestError):
        call_with_retry(fn, policy=RetryPolicy(), classify=classify_api_error, sleep=sleeps.append)
    assert sleeps == []


def test_gives_up_after_max_attempts():
    calls = []

    def fn():
        calls.append(1)
        raise api_error(anthropic.InternalServerError, 500)

    with pytest.raises(anthropic.InternalServerError):
        call_with_retry(fn, policy=RetryPolicy(max_attempts=3), classify=classify_api_error, sleep=lambda s: None)
    assert len(calls) == 3


@pytest.mark.parametrize("status,expected", [(408, True), (409, True), (429, True), (500, True), (529, True),
                                             (400, False), (401, False), (403, False), (404, False)])
def test_classify_api_status(status, expected):
    assert classify_api_error(api_error(anthropic.APIStatusError, status))[0] is expected


def test_classify_reads_retry_after():
    assert classify_api_error(api_error(anthropic.RateLimitError, 429, {"retry-after": "7"})) == (True, 7.0)


def test_connection_errors_are_transient():
    exc = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    assert classify_api_error(exc)[0] is True


def test_tool_error_classification():
    assert classify_tool_error(TransientToolError("503"))[0] is True
    assert classify_tool_error(ToolExecutionError("bad sql"))[0] is False


def test_backoff_is_capped_and_respects_retry_after():
    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=5.0)
    rng = random.Random(1)
    assert all(0 <= policy.delay(a, rng) <= 5.0 for a in range(20))
    assert policy.delay(0, rng, retry_after=12.0) == 12.0
