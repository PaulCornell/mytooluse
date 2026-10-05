"""Eval runner (PRD 005, EVAL-2..EVAL-8)."""

from __future__ import annotations

import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent.app import run_question
from agent.budget import Budget
from agent.llm import LLM
from agent.tools import WebSearchTool
from evals.graders import grade, reference_rows

QUESTIONS = Path(__file__).with_name("questions.yaml")
RESULTS_DIR = Path(__file__).with_name("results")


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    passed: bool | None  # None = skipped
    detail: str
    status: str
    steps: int
    tokens: int
    cost_usd: float
    duration_s: float
    trace: str | None
    answer: str


def load_cases(filter_: str | None = None) -> list[dict[str, Any]]:
    cases = yaml.safe_load(QUESTIONS.read_text())
    if filter_:
        cases = [c for c in cases if filter_ in c["id"] or filter_ in c.get("tags", [])]
    return cases


def check(db_path: Path, filter_: str | None = None) -> bool:
    """EVAL-5: validate every reference query without calling the model."""
    ok = True
    for case in load_cases(filter_):
        try:
            rows = reference_rows(db_path, case["grader"]["reference_sql"])
            if not rows or rows[0][0] is None:
                raise ValueError("reference query returned no value")
            print(f"  ok   {case['id']:<32} {[r[0] for r in rows][:5]}")
        except Exception as exc:
            ok = False
            print(f"  FAIL {case['id']:<32} {exc}")
    return ok


def run_eval(
    db_path: Path, llm: LLM, *, filter_: str | None = None, chaos: float = 0.0,
    seed: int | None = None, workers: int = 4, budget: Budget | None = None, prefetch_schema: bool | None = None,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RESULTS_DIR / stamp
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    search_ok = WebSearchTool.available()

    def one(case: dict[str, Any]) -> CaseResult:
        tags = case.get("tags", [])
        if "search" in tags and not search_ok:
            return CaseResult(case["id"], tags, None, "skipped: TAVILY_API_KEY not set", "skipped",
                              0, 0, 0.0, 0.0, None, "")
        trace = out_dir / "traces" / f"{case['id']}.jsonl"
        result = run_question(case["question"], db_path=db_path, llm=llm, budget=budget,
                              chaos=chaos, seed=seed, trace_path=trace, prefetch_schema=prefetch_schema)
        g = grade(case, result.answer, db_path, judge_provider=llm.provider,
                  judge_model=llm.model if llm.provider == "ollama" else None) if result.answer else None
        passed = bool(g and g.passed and result.status == "completed")
        detail = g.detail if g else f"no answer ({result.status}: {result.detail})"
        mark = "PASS" if passed else "FAIL"
        print(f"  {mark} {case['id']:<32} {result.steps} steps  ${result.cost_usd:.4f}  {detail[:90]}", file=sys.stderr)
        return CaseResult(case["id"], tags, passed, detail, result.status, result.steps, result.usage.total_tokens,
                          round(result.cost_usd, 6), round(result.duration_ms / 1000, 2), str(trace), result.answer)

    cases = load_cases(filter_)
    print(f"Running {len(cases)} cases with {llm.provider}/{llm.model} (chaos={chaos}, workers={workers}) -> {out_dir}",
          file=sys.stderr)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, cases))

    summary = summarize(results)
    meta = {"timestamp": stamp, "provider": llm.provider, "model": llm.model,
            "effort": getattr(llm, "effort", None), "chaos": chaos, "seed": seed,
            "prefetch_schema": llm.provider == "ollama" if prefetch_schema is None else prefetch_schema}
    (out_dir / "results.json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "cases": [asdict(r) for r in results]}, indent=2))
    (out_dir / "report.md").write_text(to_markdown(meta, summary, results))
    return out_dir


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    ran = [r for r in results if r.passed is not None]
    if not ran:
        return {"cases": len(results), "ran": 0}
    durations = sorted(r.duration_s for r in ran)
    by_tag: dict[str, list[bool]] = {}
    for r in ran:
        for t in r.tags:
            by_tag.setdefault(t, []).append(bool(r.passed))
    return {
        "cases": len(results),
        "ran": len(ran),
        "skipped": len(results) - len(ran),
        "accuracy": round(sum(bool(r.passed) for r in ran) / len(ran), 3),
        "accuracy_by_tag": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_tag.items())},
        "median_steps": statistics.median(r.steps for r in ran),
        "total_cost_usd": round(sum(r.cost_usd for r in ran), 4),
        "median_cost_usd": round(statistics.median(r.cost_usd for r in ran), 4),
        "p50_duration_s": durations[len(durations) // 2],
        "p90_duration_s": durations[min(len(durations) - 1, int(len(durations) * 0.9))],
        "statuses": {s: sum(r.status == s for r in ran) for s in sorted({r.status for r in ran})},
    }


def to_markdown(meta: dict[str, Any], summary: dict[str, Any], results: list[CaseResult]) -> str:
    lines = [f"# Eval report {meta['timestamp']}", "",
             f"Provider `{meta['provider']}` · model `{meta['model']}` · effort `{meta['effort']}` · "
             f"chaos {meta['chaos']} · schema prefetch {meta['prefetch_schema']}", ""]
    if summary.get("ran"):
        lines += [
            f"**Accuracy {summary['accuracy']:.0%}** ({summary['ran']} run, {summary['skipped']} skipped) · "
            f"median {summary['median_steps']} steps · median ${summary['median_cost_usd']:.4f}/question · "
            f"total ${summary['total_cost_usd']:.4f} · p50 {summary['p50_duration_s']}s · p90 {summary['p90_duration_s']}s",
            "", "By tag: " + ", ".join(f"{t} {v:.0%}" for t, v in summary["accuracy_by_tag"].items()), "",
        ]
    lines += ["| Case | Result | Status | Steps | Cost | Time | Detail |", "|---|---|---|---|---|---|---|"]
    for r in results:
        mark = "skip" if r.passed is None else ("✅" if r.passed else "❌")
        lines.append(f"| {r.id} | {mark} | {r.status} | {r.steps} | ${r.cost_usd:.4f} | {r.duration_s}s | "
                     f"{r.detail.replace('|', '/')[:120]} |")
    return "\n".join(lines) + "\n"


def compare(a: Path, b: Path) -> str:
    """EVAL-8: compare two results.json files."""
    ra, rb = json.loads(a.read_text()), json.loads(b.read_text())
    sa, sb = ra["summary"], rb["summary"]
    lines = [f"{'metric':<18}{'A':>12}{'B':>12}"]
    for key in ("accuracy", "median_steps", "median_cost_usd", "total_cost_usd", "p50_duration_s"):
        lines.append(f"{key:<18}{sa.get(key, '-'):>12}{sb.get(key, '-'):>12}")
    cases_a = {c["id"]: c["passed"] for c in ra["cases"]}
    changed = [(c["id"], cases_a.get(c["id"]), c["passed"]) for c in rb["cases"] if cases_a.get(c["id"]) != c["passed"]]
    if changed:
        lines.append("\nchanged cases:")
        lines += [f"  {cid}: {pa} -> {pb}" for cid, pa, pb in changed]
    return "\n".join(lines)
