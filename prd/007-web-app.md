# PRD 007 — Local web app

| | |
|---|---|
| Status | Implemented |
| Depends on | 001, 004, 006 |
| Code | `agent/web/server.py`, `agent/web/static/`, `askdata serve` |

## 1. Problem

The CLI shows one line per event, which works for developers but hides most of what makes an agent
an agent: the conversation the model receives, why the loop continues or stops, and what the harness
does between model calls. People learning how tool-using agents work need to *watch* the loop, with
each step explained.

## 2. Approach

`askdata serve` starts a small FastAPI server on localhost and opens a single page. The agent runs in a
background thread. The tracer already sends every event to listeners (PRD 004), so the server adds
one listener that pushes events to the browser over Server-Sent Events. The page renders each event as
it arrives, with a plain-language explanation of what just happened and why.

**Why Python and not Express or Next.js:** the agent is Python. A Node front end would need a second
runtime plus a bridge between the two processes, which means more setup for anyone who clones the
repo. One FastAPI process and one page of plain HTML, CSS and JavaScript (no build step) keep setup to
`pip install`.

No new agent logic lives in the web layer. It shows exactly what's in the trace, so anything the page
shows can be reproduced with `askdata view` or `askdata replay`.

## 3. Requirements

### Functional

| ID | Priority | Requirement |
|---|---|---|
| WEB-1 | Must | `askdata serve` starts the app at `http://127.0.0.1:8000/` and opens a browser tab (`--no-browser` to skip). |
| WEB-2 | Must | The page streams a run's events live and renders each type: setup, schema prefetch, request contents, model responses (reasoning, text, tool calls, `stop_reason`, token usage), tool calls and results, retries, guards and the final answer. |
| WEB-3 | Must | "Explain each step" mode (on by default; can be turned off) adds a plain-language explanation to every event: what it is, why it happened and what the loop does next. |
| WEB-4 | Must | Only one run at a time (a local model has one GPU). A second request gets HTTP 409 and a clear message, before the server does any other work for it (such as an Ollama preflight check). |
| WEB-5 | Must | A loop diagram (check budget → call model → read `stop_reason` → run tools → send results back → answer) highlights the current stage, and a stats bar shows steps, tool calls, errors, retries, tokens, cost and time. |
| WEB-9 | Must | A "What the model sees" view shows the full conversation as sent to the model: system prompt, user messages, assistant replies and tool results (from `messages_added`, OBS-9). |
| WEB-10 | Should | A "Raw events" view lists every trace event as JSON. |
| WEB-11 | Must | Past runs (from `runs/*.jsonl`) are listed and can be reopened. Each run has a link (`/#run=<id>`). |
| WEB-12 | Must | The page shows provider status (from the Ollama preflight, PRD 006), database status and web search status, with fix-it messages for problems. |
| WEB-13 | Should | Options: provider, model, max steps, time limit, schema prefetch, chaos level, and chaos seed (the same seed reproduces the same failures). |
| WEB-14 | Must | Streams resume after a dropped connection, using `Last-Event-ID`, without missing or repeating events. |
| WEB-15 | Must | The layout works at phone width (360–400 px), in light and dark mode, and respects reduced-motion settings. |

### Security

| ID | Priority | Requirement |
|---|---|---|
| WEB-6 | Must | The server binds to 127.0.0.1 by default. Binding anywhere else prints a warning, because runs can execute Python. |
| WEB-7 | Must | Requests with an unexpected `Host` header are refused (protects against DNS rebinding). |
| WEB-8 | Must | POSTs with a cross-origin `Origin` header, or without `Content-Type: application/json`, are refused, so another website can't start a run from the user's browser. |
| WEB-16 | Must | The page inserts all event data as text, never as HTML, because tool results can contain untrusted text (for example, web search snippets). |
| WEB-17 | Must | Run IDs in URLs are validated against a strict pattern before any file is read (no path traversal). |

## 4. Non-goals

- Hosting on the public internet, accounts, or multi-user use.
- Cancelling a run in progress. Budgets bound every run; a Stop button is a possible follow-up.
- Editing or re-running individual steps.

## 5. Acceptance criteria

All in `tests/test_web.py` unless noted:

- A scripted run streams every event in order, from `run_start` to `run_end`, including `messages_added` with the tool results.
- Reconnecting with `Last-Event-ID: 3` resumes at event 4.
- A second run while one is active gets 409; after it finishes, a new run is accepted.
- A run is listed and replayed from disk by a freshly started server.
- A preflight failure returns 400 with the fix-it message; a crash inside a run becomes a `server_error` event, and the server accepts new runs afterwards.
- Cross-origin POSTs (403), non-JSON POSTs (415), unexpected hosts (400) and malformed run IDs (404) are refused.
- Verified manually against Ollama 0.35.1 with `qwen2.5:3b`: live runs (including chaos mode) render correctly in light and dark mode and at 390 px width.

Browser tests (`tests/e2e/test_web_app.py`, Playwright in headless Chromium, with a scripted model):

- WEB-2, WEB-3, WEB-5: a full run shows Setup, Step 1 and Step 2 with explanations, a results table, the loop diagram ending on "Answer", and correct stats; while the model is thinking, a waiting timer and the "Call model" stage show.
- Error, retry and budget events render with their explanations, including a seeded chaos run (WEB-13) and a 1-step budget stop.
- WEB-9, WEB-10: the "What the model sees" and "Raw events" views.
- WEB-3: the explain toggle hides explanations and is remembered across reloads.
- WEB-11: past runs reopen from the list and from their links.
- WEB-4: a second question during a run shows "already in progress".
- WEB-12: an unavailable provider shows its fix-it message.
- WEB-16: model output containing HTML and script is shown as text and never executes.
- WEB-15: no horizontal scrolling at 390 px; dark mode follows the system setting.
