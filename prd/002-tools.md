# PRD 002 — Tools

| | |
|---|---|
| Status | Implemented |
| Code | `agent/tools/` |

## 1. Problem

The agent needs real tools with real failure modes. Each tool must have a precise schema,
validate its input, enforce a timeout, and return errors the model can act on.

## 2. Tool contract

Every tool has:

- a `name` and a `description` written for the model (what it does, when to use it, its limits);
- an input model (Pydantic) that generates the JSON schema and validates the input before the
  tool runs;
- a `timeout_s`;
- a `run(input) -> str` method that raises typed errors (PRD 003):
  - `ToolInputError` / `ToolExecutionError`: the model can fix these. They are returned as `is_error`.
  - `TransientToolError`: retried by the harness. The model sees it only if every retry fails.

## 3. Tools

### 3.1 `get_schema`

Returns tables, columns, types, row counts and a few sample distinct values for low-cardinality
text columns. The model should call this first.

### 3.2 `run_sql`

| ID | Priority | Requirement |
|---|---|---|
| TOOL-1 | Must | The connection is opened read-only (`mode=ro` URI plus `PRAGMA query_only=ON`). |
| TOOL-2 | Must | A SQLite authorizer allows only `SELECT`/`READ`/function operations. Writes, `ATTACH` and `PRAGMA` writes are denied. |
| TOOL-3 | Must | A query timeout is enforced with a progress handler. A timeout is returned to the model as fixable ("simplify or add a filter"). |
| TOOL-4 | Must | Results are capped (default 200 rows), and the output says when they were truncated. |
| TOOL-5 | Must | SQLite error messages are passed to the model verbatim, with a hint pointing to `get_schema`. |
| TOOL-6 | Should | Results are formatted as a compact Markdown table. |

### 3.3 `web_search`

| ID | Priority | Requirement |
|---|---|---|
| TOOL-7 | Must | Uses the Tavily Search API (`TAVILY_API_KEY`). If no key is configured, the tool is left out of the tool list rather than failing at runtime. |
| TOOL-8 | Must | HTTP 429, HTTP 5xx and network timeouts raise `TransientToolError`. Other 4xx errors raise `ToolExecutionError`. |
| TOOL-9 | Must | Results include title, URL and a snippet, and are wrapped in an "untrusted content" notice. |
| TOOL-10 | Should | Results are truncated to keep tool output under ~4k tokens. |

### 3.4 `run_python`

| ID | Priority | Requirement |
|---|---|---|
| TOOL-11 | Must | Code runs in a separate subprocess (`python -I`) in a per-run working directory. |
| TOOL-12 | Must | The child environment is cleared, so API keys and secrets are never inherited. |
| TOOL-13 | Must | Wall-clock timeout, CPU-time limit and output-file-size limit are enforced. |
| TOOL-14 | Must | On macOS, the process runs under `sandbox-exec` with network access denied and writes limited to the working directory. On other platforms this is documented as a gap. |
| TOOL-15 | Must | The child can read the SQLite database via the `AGENT_DB_PATH` environment variable (read-only). |
| TOOL-16 | Must | stdout/stderr are captured and truncated. A non-zero exit returns the traceback as a fixable error. |

## 4. Threat model (sandbox)

The sandbox protects against **accidents and prompt-injected code from search results**, not
against a determined attacker with a kernel exploit. Specifically:

- **In scope:** exfiltrating secrets through environment variables (blocked); network access
  (blocked on macOS); writing outside the run directory (blocked on macOS); fork bombs, infinite
  loops and runaway output (limited by rlimits and timeouts).
- **Out of scope:** container-grade isolation. If this were deployed for untrusted users, code
  should run in a microVM or gVisor container. That is a follow-up, not part of this project.

## 5. Acceptance criteria

- `INSERT`, `DROP`, `ATTACH` and `PRAGMA writable_schema` are rejected by `run_sql` (`tests/test_sql_tool.py`).
- A query that runs longer than the timeout is interrupted and returns a fixable error.
- `run_python` cannot see `ANTHROPIC_API_KEY`, and `while True: pass` is killed within the timeout
  (`tests/test_python_tool.py`).
- An invalid tool input (wrong type, missing field) returns `is_error` with the validation
  message, and the tool does not run (`tests/test_registry.py`).
- Search HTTP 429 raises a transient error that honors `retry-after`; HTTP 400 and empty results are
  model-fixable; results are wrapped as untrusted (`tests/test_search_tool.py`).
