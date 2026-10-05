# Product Requirements Documents

These PRDs describe **Ask the Data**, a multi-step tool-using agent that answers analytical
questions by combining a SQLite database, web search, and sandboxed Python execution. It runs on a free local model
through Ollama by default, or on Claude.

| PRD | Title | Status |
|---|---|---|
| [000](000-product-overview.md) | Product overview | Approved |
| [001](001-agent-loop.md) | Agent loop | Implemented |
| [002](002-tools.md) | Tools: SQL, web search, Python execution | Implemented |
| [003](003-reliability.md) | Reliability: error handling, retries, budgets, chaos | Implemented |
| [004](004-observability.md) | Observability: trace log, viewer, replay | Implemented |
| [005](005-evaluation.md) | Evaluation harness | Implemented |
| [006](006-local-models.md) | Local models via Ollama (no-cost default) | Implemented |
| [007](007-web-app.md) | Local web app that explains each step | Implemented |

## Conventions

- Requirements are numbered `<PRD>-<n>` (e.g. `REL-3`) so that code, tests and reviews can
  reference them.
- **Must** requirements block a release; **Should** requirements are expected but negotiable;
  **Could** requirements are explicitly optional.
- Each PRD ends with acceptance criteria that map to automated tests where possible.
- Status values: Draft → Approved → Implemented → Superseded.
