# PRD 004 — Observability: trace log, viewer, replay

| | |
|---|---|
| Status | Implemented |
| Code | `agent/trace.py`, `agent/viewer.py`, `agent/replay.py` |

## 1. Problem

An agent run is non-deterministic and spans many calls. Without a full record you can't
debug a bad answer, explain a cost spike, or turn a failure into a regression test.

## 2. Trace format

One JSON object per line (JSONL), written and flushed as events happen, so a crashed run
still leaves a usable trace.

Common fields: `run_id`, `seq` (monotonic), `ts` (ISO-8601 UTC), `step`, `type`.

| `type` | Extra fields |
|---|---|
| `run_start` | `question`, `provider`, `model`, `config`, `tools`, `system` |
| `messages_added` | `messages` (the messages this request adds to the conversation, as JSON), `total` |
| `model_request` | `message_count`, `attempt` |
| `model_response` | `response` (full message dict), `stop_reason`, `usage`, `cost_usd`, `duration_ms`, `request_id` |
| `tool_call` | `tool_use_id`, `name`, `input` |
| `tool_result` | `tool_use_id`, `name`, `content`, `is_error`, `error_kind`, `attempts`, `duration_ms` |
| `retry` | `target` (`model` or tool name), `attempt`, `delay_s`, `error`, `injected` |
| `guard` | `reason`, `detail` (loop guard and budget notices) |
| `run_end` | `status`, `answer`, `steps`, `usage`, `cost_usd`, `duration_ms` |

## 3. Requirements

| ID | Priority | Requirement |
|---|---|---|
| OBS-1 | Must | Every model call, tool call, tool result, retry and guard decision produces an event. |
| OBS-2 | Must | Traces go to `runs/<run_id>.jsonl` by default. `--trace PATH` overrides the location. |
| OBS-3 | Must | Model responses are stored in full (`to_dict()`), so they can be replayed. |
| OBS-4 | Must | `askdata view TRACE` renders a self-contained HTML timeline: one card per step, with tool inputs and outputs, errors and retries highlighted, and totals for tokens, cost and time. |
| OBS-5 | Must | `askdata replay TRACE` reruns the loop with model responses and tool results served from the trace, with no network access, and checks that the loop produces the same sequence of tool calls and the same final answer. |
| OBS-6 | Must | Replay fails loudly with the first point of divergence if the loop's behavior has changed. |
| OBS-7 | Should | Secrets (API keys) never appear in traces. The config snapshot excludes environment variables. |
| OBS-8 | Should | The CLI prints a live one-line-per-event summary to stderr (`--quiet` turns it off). |
| OBS-9 | Should | Before each model call, a `messages_added` event records the messages that request adds, so a trace shows exactly what the model saw (used by the web app's Conversation view, PRD 007). |

## 4. Acceptance criteria

- A fixture trace in `tests/fixtures/` replays without network access, and the test passes (`tests/test_replay.py`).
- When the loop's tool calls no longer match the recorded ones, replay fails with a divergence message (`tests/test_replay.py`).
- `askdata view` produces an HTML file with no external network dependencies (`tests/test_viewer.py`).
