# PRD 001 — Agent loop

| | |
|---|---|
| Status | Implemented |
| Depends on | 002, 003, 004 |
| Code | `agent/loop.py`, `agent/llm.py` |

## 1. Problem

The agent loop is the main thing this project demonstrates. It must be explicit and short enough
to read in one sitting, and it must still handle every response shape the Messages API can
return.

## 2. Design

```
messages = [user question]
loop:
    check budgets                         → stop if exceeded
    response = llm.create(messages, tools)  (with retries, traced)
    append response.content as-is          (append-only history)
    switch response.stop_reason:
        end_turn       → done, final answer = text blocks
        tool_use       → run every tool_use block, append ONE user message of tool_results
        max_tokens     → done, partial (flagged)
        pause_turn     → continue (re-send)
        refusal        → done, refused (flagged)
```

## 3. Requirements

| ID | Priority | Requirement |
|---|---|---|
| LOOP-1 | Must | The loop calls the Anthropic Messages API directly, not a tool runner or agent framework. |
| LOOP-2 | Must | Assistant content is appended to history unchanged (including thinking and fallback blocks), so history stays append-only. |
| LOOP-3 | Must | All `tool_use` blocks in one response are executed, and all their `tool_result`s are returned in a single user message, in the same order. |
| LOOP-4 | Must | Every `tool_use` gets a `tool_result`, even when the tool fails or is blocked (`is_error: true`). |
| LOOP-5 | Must | `stop_reason` is checked before content is read; `refusal`, `max_tokens` and `pause_turn` are handled explicitly. |
| LOOP-6 | Must | The run returns a structured `RunResult`: answer, status, steps, usage, cost, run ID and trace path. |
| LOOP-7 | Must | The model client is behind an `LLM` protocol so that a replay client, or a local model (PRD 006), can stand in for the Claude API. |
| LOOP-8 | Should | Server-side refusal fallback (`fallbacks: "default"`) is enabled for models that support it. |
| LOOP-9 | Should | The system prompt and tool list are kept byte-stable across turns so that prompt caching works. |
| LOOP-10 | Should | Independent tool calls in one turn may run concurrently, and results are returned in call order. |
| LOOP-11 | Could | Streaming output to the terminal. (Deferred: non-streaming keeps the loop simpler to read.) |

## 4. Run statuses

| Status | Meaning |
|---|---|
| `completed` | `end_turn` with a text answer |
| `budget_exceeded` | A step, token, cost or time budget tripped (PRD 003) |
| `refused` | Final `stop_reason == "refusal"` after any fallback |
| `truncated` | `max_tokens` on the final turn |
| `aborted` | Repeated-call guard tripped or an unrecoverable error occurred |
| `error` | Non-retryable API error (e.g. authentication) |

## 5. Acceptance criteria

- With a scripted fake LLM that issues bad SQL, receives the error and issues corrected SQL,
  the run completes and the trace shows both attempts (`tests/test_loop.py`).
- With two `tool_use` blocks in one response, exactly one user message containing two
  `tool_result`s follows (`tests/test_loop.py`).
- A refusal response ends the run with status `refused` and no exception.
- `agent/loop.py` stays at or below 280 lines.
