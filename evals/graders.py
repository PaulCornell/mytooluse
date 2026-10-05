"""Graders for eval answers (PRD 005 §2)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from agent.llm import OLLAMA_HOST

_NUMBER = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
DEFAULT_JUDGE_MODELS = {"anthropic": "claude-opus-5-5", "ollama": "askdata-local"}
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Grade:
    passed: bool
    detail: str


def extract_numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text)]


def reference_rows(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def grade_numeric(answer: str, expected: float, rel_tol: float = 0.01, abs_tol: float = 0.0) -> Grade:
    tolerance = max(abs(expected) * rel_tol, abs_tol)
    numbers = extract_numbers(answer)
    for n in numbers:
        if abs(n - expected) <= tolerance:
            return Grade(True, f"found {n:g} (expected {expected:g} ± {tolerance:g})")
    shown = ", ".join(f"{n:g}" for n in numbers[:8]) or "none"
    return Grade(False, f"expected {expected:g} ± {tolerance:g}; numbers in answer: {shown}")


def grade_contains_all(answer: str, expected: list[Any]) -> Grade:
    haystack = answer.lower()
    missing = [str(v) for v in expected if str(v).lower() not in haystack]
    if missing:
        return Grade(False, f"missing: {', '.join(missing)}")
    return Grade(True, f"found all of: {', '.join(str(v) for v in expected)}")


class Verdict(BaseModel):
    passed: bool
    reason: str


def grade_judge(question: str, answer: str, rubric: str, *, provider: str = "anthropic",
                model: str | None = None) -> Grade:
    model = model or DEFAULT_JUDGE_MODELS[provider]
    prompt = ("You are grading an analyst's answer against a rubric. Apply the rubric literally.\n\n"
              f"<question>{question}</question>\n<answer>{answer}</answer>\n<rubric>{rubric}</rubric>")
    if provider == "anthropic":
        response = anthropic.Anthropic().messages.parse(
            model=model, max_tokens=2000, output_format=Verdict, messages=[{"role": "user", "content": prompt}],
        )
        verdict = response.parsed_output
        if verdict is None:
            return Grade(False, f"judge returned no verdict (stop_reason={response.stop_reason})")
        return Grade(verdict.passed, f"judge: {verdict.reason}")

    # Local judge: no structured-output guarantee, so ask for JSON and parse defensively.
    client = anthropic.Anthropic(base_url=OLLAMA_HOST, api_key="ollama", timeout=600.0)
    response = client.messages.create(
        model=model, max_tokens=500,
        system='Reply with only a JSON object: {"passed": true or false, "reason": "<one sentence>"}',
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    try:
        verdict = Verdict.model_validate(json.loads(_JSON_OBJECT.search(raw).group(0)))
    except (AttributeError, json.JSONDecodeError, ValidationError):
        return Grade(False, f"local judge returned unparseable output: {raw[:120]!r}")
    return Grade(verdict.passed, f"local judge ({model}): {verdict.reason}")


def grade(case: dict[str, Any], answer: str, db_path: Path, *, judge_provider: str = "anthropic",
          judge_model: str | None = None) -> Grade:
    g = case["grader"]
    rows = reference_rows(db_path, g["reference_sql"]) if g.get("reference_sql") else []
    match g["type"]:
        case "numeric":
            return grade_numeric(answer, float(rows[0][0]), g.get("rel_tol", 0.01), g.get("abs_tol", 0.0))
        case "contains_all":
            return grade_contains_all(answer, [r[0] for r in rows])
        case "judge":
            reference = rows[0][0] if rows else ""
            return grade_judge(case["question"], answer, g["rubric"].replace("{reference}", str(reference)),
                               provider=judge_provider, model=judge_model)
    raise ValueError(f"unknown grader type {g['type']!r}")
