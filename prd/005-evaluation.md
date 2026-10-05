# PRD 005 — Evaluation harness

| | |
|---|---|
| Status | Implemented |
| Code | `evals/` |

## 1. Problem

"It answered my demo question" isn't evidence. Prompt, model and tool changes need a fixed
question set with automatic grading that reports quality, efficiency and cost together.

## 2. Question set

`evals/questions.yaml`. Each case has an `id`, a `question`, `tags` and a `grader`:

| Grader | How it works | Used for |
|---|---|---|
| `numeric` | Runs `reference_sql` (a single value) against the live DB at eval time, then passes if any number in the answer is within `tolerance` (relative). | Counts, averages, percentages |
| `contains_all` | Runs `reference_sql` and passes if every value in the first column appears in the answer (case-insensitive). | Top-N lists, names |
| `judge` | An LLM judge scores the answer against a `rubric`, returning pass/fail with a reason (structured output). | Questions that need web search or interpretation |

Answers are computed from reference SQL at eval time, so the eval stays correct when the
dataset is refreshed.

## 3. Requirements

| ID | Priority | Requirement |
|---|---|---|
| EVAL-1 | Must | At least 15 questions. At least 10 are database-only, at least 3 need search and at least 3 need Python. |
| EVAL-2 | Must | `askdata eval` runs all questions (or a `--filter` by id or tag) and writes `evals/results/<timestamp>/results.json` and `report.md`, plus one trace per question. |
| EVAL-3 | Must | The report includes, per question: pass/fail, grader detail, steps, tokens, cost, duration, status and trace path. It also includes aggregates: accuracy, median steps, total cost and p50/p90 duration. |
| EVAL-4 | Must | Each question's trace is kept so failures can be inspected in the viewer. |
| EVAL-5 | Must | Reference SQL is validated by `askdata eval --check` without calling the model. |
| EVAL-6 | Should | `--chaos`, `--provider` and `--model` pass through, so you can compare robustness, providers and models. |
| EVAL-7 | Should | Questions run concurrently (`--workers N`; default 4 for Claude, 1 for local models). |
| EVAL-8 | Could | Compare two result files (`--compare A.json B.json`). |

## 4. Acceptance criteria

- `askdata eval --check` passes on the loaded dataset.
- Unit tests cover the `numeric` and `contains_all` graders, including tolerance and formatted
  numbers such as `12,345` and `41.2%` (`tests/test_graders.py`).
