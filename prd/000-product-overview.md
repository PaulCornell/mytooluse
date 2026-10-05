# PRD 000 — Product overview: Ask the Data

| | |
|---|---|
| Status | Approved |
| Owner | Paul Cornell |
| Last updated | 2026-10-05 |

## 1. Summary

Ask the Data is a command-line agent that answers natural-language analytical questions about
a real dataset (Washington State electric-vehicle registrations). To answer, it plans and
executes a sequence of tool calls — inspecting a SQLite schema, running SQL, searching the web,
and running Python in a sandbox — then writes an answer that cites the evidence it used.

The project exists to demonstrate production-minded agent engineering: a hand-written agent
loop (no framework), explicit error handling and retries, hard budgets, a complete trace log,
deterministic replay, fault injection, and an evaluation suite.

## 2. Problem

Most agent demos wrap a framework call and show a happy-path transcript. They don't show what
happens when SQL is wrong, a search API rate-limits, code hangs, or the model loops. Reviewers
evaluating AI-engineering skill need to see:

- that the author understands the request → tool call → result cycle at the protocol level;
- that failures are classified and handled deliberately;
- that the system can be observed, debugged, reproduced and measured.

## 3. Goals

1. Answer multi-step questions that need two or more tools, with cited evidence.
2. Make the agent loop readable in a single file of roughly 200 lines or fewer.
3. Recover from model-correctable errors (bad SQL, Python exceptions) without human help.
4. Absorb transient infrastructure failures (rate limits, timeouts) invisibly to the model.
5. Never exceed configured step, token, cost or wall-clock budgets.
6. Record every run as a replayable trace; render traces as a human-readable timeline.
7. Quantify quality with a repeatable eval suite that reports accuracy, steps and cost.
8. Let anyone who clones the repo run everything at no cost, using a local open-weight model (PRD 006).

## 4. Non-goals

- A hosted or multi-user web service. The web app (PRD 007) runs on localhost only.
- Writing to the database. All data access is read-only.
- Production-grade isolation for untrusted multi-tenant code (see PRD 002 threat model).
- Supporting arbitrary LLM providers. Two are supported, Claude and local models via Ollama, and both
  speak the Anthropic Messages API (PRD 006).

## 5. Users and scenarios

| User | Scenario |
|---|---|
| Portfolio reviewer / hiring manager | Reads the README and `loop.py`, opens a trace in the viewer, runs the tests without an API key. |
| Analyst (demo persona) | Asks "Which counties have the highest share of PHEVs, and has state policy targeted them?" and gets a cited answer. |
| Developer (the author) | Runs evals after a prompt change and compares accuracy/cost; replays a failing trace to debug. |

## 6. Scope overview

| Area | PRD |
|---|---|
| Agent loop, message handling, stop conditions | [001](001-agent-loop.md) |
| `get_schema`, `run_sql`, `web_search`, `run_python` tools | [002](002-tools.md) |
| Error taxonomy, retries, budgets, loop guard, chaos mode | [003](003-reliability.md) |
| JSONL trace, HTML viewer, replay | [004](004-observability.md) |
| Eval questions, graders, reports | [005](005-evaluation.md) |
| Free local models via Ollama | [006](006-local-models.md) |
| Local web app that explains each step | [007](007-web-app.md) |

## 7. Data

- **Source:** Washington State Department of Licensing, *Electric Vehicle Population Data*
  (data.wa.gov, dataset `f6w7-q2d2`), ~300k rows, public domain.
- **Storage:** loaded into SQLite table `vehicles` by `askdata load-data` (`agent/data.py`).
- Anything not in the dataset (charging infrastructure, incentives, news) comes from web search.

## 8. Success metrics

| Metric | Target |
|---|---|
| Eval accuracy (database-answerable questions) | ≥ 85% |
| Median steps per question | ≤ 6 |
| Median cost per question (Claude Sonnet) | ≤ $0.05 |
| Cost to clone and run everything locally | $0 |
| Runs completing under `--chaos 0.2` | ≥ 90% of the no-chaos pass rate |
| Unit tests runnable with no network and no API key | 100% |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Model writes plausible but wrong SQL | Schema tool with sample values; eval suite with reference SQL. |
| Prompt injection via search results | Search results are wrapped as untrusted data; tools are read-only; Python sandbox has no network or secrets. |
| Cost runaway in loops | Hard budgets and a repeated-call guard (PRD 003). |
| Dataset changes over time | Eval answers are computed from reference SQL at eval time, not hard-coded. |

## 10. Milestones

1. PRDs approved.
2. Loop plus SQL tool, with tracing.
3. Search and Python tools, with the sandbox.
4. Reliability features: retries, budgets, chaos mode.
5. Viewer and replay.
6. Eval suite, plus the README with results.
