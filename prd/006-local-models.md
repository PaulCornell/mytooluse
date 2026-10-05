# PRD 006 — Local models via Ollama

| | |
|---|---|
| Status | Implemented |
| Depends on | 001 |
| Code | `agent/llm.py` (`OllamaLLM`), `agent/cli.py` (`setup-local`), `ollama/Modelfile` |

## 1. Problem

Anyone who clones the repository should be able to run the agent, the evals and the trace tools
without creating an account or paying for API usage. With Claude as the only model, the first
`askdata ask` needs a paid API key.

## 2. Approach

Ollama serves open-weight models locally and exposes an **Anthropic Messages-compatible endpoint**
(`POST /v1/messages`). The existing Anthropic SDK can talk to it by changing `base_url`, so the loop,
tools, error handling, traces, viewer and replay need no provider-specific code. The provider
differences are kept in one class, `OllamaLLM`, which implements the same `LLM` protocol as
`AnthropicLLM`.

This was checked against Ollama 0.35.1 before building. Tool calls, `is_error` tool results,
`tool_choice: none` and multi-turn history all round-trip, and responses parse as `BetaMessage`.

## 3. Requirements

| ID | Priority | Requirement |
|---|---|---|
| LOCAL-1 | Must | `--provider ollama` is the default (overridable with `ASKDATA_PROVIDER`). Claude is opt-in with `--provider anthropic`. |
| LOCAL-2 | Must | `askdata setup-local` pulls a small tool-capable base model and creates `askdata-local` with a 16k context. The repo ships the same definition as `ollama/Modelfile`. |
| LOCAL-3 | Must | Before a run, a preflight check fails fast with a fix-it message if Ollama isn't reachable, the model isn't installed, or the model lacks tool-calling capability. |
| LOCAL-4 | Must | Preflight warns when the model has no `num_ctx` or one below 12k tokens. Ollama's server default (often 4,096) silently truncates the start of the conversation. |
| LOCAL-5 | Must | Local runs report $0.00 cost. Unlisted Claude models are still priced (as Opus), so cost budgets fail safe. |
| LOCAL-6 | Should | When a model call's prompt reaches 90% or more of the known context window, a `guard` event (`context_near_limit`) is traced. |
| LOCAL-7 | Must | Claude-only parameters (adaptive thinking, effort, prompt caching, refusal fallbacks, beta headers) are never sent to the local endpoint. |
| LOCAL-8 | Should | Evals run one question at a time by default with a local model (one GPU). Search questions use the local model as an LLM judge, with defensive JSON parsing in place of structured outputs. |
| LOCAL-9 | Must | The unit tests and `askdata replay` need neither Ollama nor an API key. |
| LOCAL-10 | Should | With local models, the harness runs `get_schema` before the first model call and puts the result in the first message (traced as a step-0 tool call, so it's visible and replayable). `--no-prefetch-schema` turns this off; it's off by default for Claude. |

## 4. Non-goals

- OpenAI-compatible or other hosted providers.
- Matching Claude's answer quality. Small local models are a free way to run and explore the
  system, and the evals measure the gap instead of hiding it.

## 5. Hardware guidance

| RAM | Suggested base model | Notes |
|---|---|---|
| 8 GB | `qwen2.5:3b` (default, ~2 GB) | Measured: 18% eval accuracy (3/17) with schema prefetch off, median 3 steps, p50 18s per question. Often guesses table/column names and makes case-sensitivity mistakes in SQL. |
| 8 GB | `qwen3.5:4b` (~3.3 GB) | Tried and rejected as the default. At a 16k context it loaded at 4.0 GB, only 79% on the GPU, and its first call ran past a 10-minute timeout. At 8k it fit on the GPU (3.2 GB) and wrote correct SQL, but with ordinary apps open the machine was swapping and generation ran at ~1 token/s (65–93 s per step). Turning off thinking (`thinking: {"type": "disabled"}`, which Ollama's endpoint honors) helped, but not enough. |
| 16 GB+ | `qwen3.5:4b` | Recommended upgrade (newer, trained for tool calling, has a thinking mode). Not yet measured with the eval suite. |

Use `askdata setup-local --base <model>` to build `askdata-local` on a different base model.

## 6. Acceptance criteria

- With no Ollama running, `askdata ask` exits with code 2 and tells the user how to start it (`tests/test_llm.py`).
- A model without `num_ctx` produces a context warning, and a model without tools is rejected (`tests/test_llm.py`).
- Requests to Ollama contain only `model`, `max_tokens`, `system`, `messages`, `tools` (and `tool_choice` when set), with no beta header (`tests/test_llm.py`).
- With prefetch on, the first message contains the schema and the trace shows a step-0 `get_schema` call that replays (`tests/test_loop.py`, `tests/test_replay.py`).
- A real local run completes, renders in the viewer and replays successfully (verified manually on Ollama 0.35.1 with `qwen2.5:3b`).
