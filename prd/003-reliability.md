# PRD 003 — Reliability: errors, retries, budgets, chaos

| | |
|---|---|
| Status | Implemented |
| Code | `agent/errors.py`, `agent/retry.py`, `agent/budget.py`, `agent/chaos.py` |

## 1. Problem

An agent runs in an environment where things fail. Different failures need different handling:
some should be invisible retries, some should go back to the model so it can correct itself, and
some should stop the run. Getting this wrong either wastes tokens (the model debugs a rate
limit) or hides bugs (retrying a malformed request forever).

## 2. Error taxonomy

| Class | Examples | Who handles it | Visible to the model? |
|---|---|---|---|
| **Transient** | API 429/5xx/529, connection reset, search timeout | Harness: retry with backoff | Only if retries are exhausted |
| **Model-fixable** | SQL syntax error, unknown column, Python exception, invalid tool input, query timeout | Model: gets `is_error: true` with an actionable message | Yes |
| **Fatal** | Authentication failure, bad request, budget exceeded | Harness: stop the run with a status | No |

## 3. Requirements

### Retries

| ID | Priority | Requirement |
|---|---|---|
| REL-1 | Must | The harness owns API retries. The SDK's own retries are disabled (`max_retries=0`), so every attempt is visible in the trace. |
| REL-2 | Must | Exponential backoff with full jitter: `sleep = uniform(0, min(cap, base·2^attempt))`. Default base 1s, cap 30s, 4 attempts. |
| REL-3 | Must | A `retry-after` header, when present, sets the minimum delay. |
| REL-4 | Must | Only transient errors are retried. 400/401/403/404 are never retried. |
| REL-5 | Must | Each retry emits a `retry` trace event with the attempt number, delay and error. |
| REL-6 | Must | Tool calls use the same retry policy for `TransientToolError` and timeouts. |
| REL-7 | Should | The retry `sleep` function can be injected, so tests run instantly. |

### Budgets

| ID | Priority | Requirement |
|---|---|---|
| REL-8 | Must | Configurable limits: max steps (default 12), max total tokens (default 400k), max cost in USD (default $0.50) and max wall-clock time (default 300s). |
| REL-9 | Must | Budgets are checked before each model call. Exceeding one ends the run with `budget_exceeded` and names the budget that tripped. |
| REL-10 | Must | Cost is computed from `usage` (input, output, cache read, cache write) and a per-model price table. |
| REL-11 | Should | On the last allowed step the model gets a notice to answer with what it has, so it doesn't stop silently. |
| REL-17 | Must | Each model call (including retries) gets a request timeout equal to the wall-clock budget remaining, and no new attempt starts once it's used up. Without this, one slow call could overrun `max_wall_s` by the client timeout times the retry count. |

### Loop guard

| ID | Priority | Requirement |
|---|---|---|
| REL-12 | Must | Identical tool calls (same name and canonical JSON input) are counted per run. On the 3rd identical call the tool isn't executed; the model gets an error telling it to change approach. |
| REL-13 | Must | On the 5th identical call the run is aborted. |

### Chaos mode

| ID | Priority | Requirement |
|---|---|---|
| REL-14 | Must | `--chaos P` injects a transient failure into each tool attempt with probability P (a mix of transient errors and simulated timeouts). |
| REL-15 | Must | Chaos uses a seeded RNG (`--seed`) so failures are reproducible. |
| REL-16 | Must | Injected failures are marked `injected: true` in the trace. |

## 4. Acceptance criteria

- The retry helper retries a 429 three times and then succeeds; it does not retry a 400 (`tests/test_retry.py`).
- Backoff delays never exceed the cap and respect `retry-after`.
- The cost budget trips when the accumulated cost passes the limit (`tests/test_budget.py`).
- Model-call timeouts shrink with elapsed time, and a run whose budget runs out during retries ends as `budget_exceeded` (`tests/test_loop.py`).
- With `chaos=0.5` and a fixed seed, a scripted run still completes, and the trace contains `retry` events marked as injected (`tests/test_loop.py`).
- The 3rd identical failing call is blocked, and the 5th aborts the run (`tests/test_loop.py`).
