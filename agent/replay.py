"""Deterministic replay of a recorded trace (PRD 004, OBS-5, OBS-6).

The real loop runs again, but model responses and tool results are served from the trace.
No network, no API key. If the loop now behaves differently (it skips a tool call, calls
tools in a different order, or reaches a different final state), replay reports the first
divergence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic.types.beta import BetaMessage

from agent.loop import Agent, config_from_dict
from agent.tools.base import ToolOutcome
from agent.trace import Tracer, read_trace


class ReplayDivergence(AssertionError):
    pass


class ReplayLLM:
    context_window = None

    def __init__(self, provider: str, model: str, responses: list[dict[str, Any]]):
        self.provider = provider
        self.model = model
        self._responses = list(responses)
        self.calls = 0

    def create(self, *, system, messages, tools, tool_choice=None, timeout=None) -> BetaMessage:
        if self.calls >= len(self._responses):
            raise ReplayDivergence(
                f"loop requested model call #{self.calls + 1}, but the trace only recorded {len(self._responses)}"
            )
        response = BetaMessage.model_validate(self._responses[self.calls])
        self.calls += 1
        return response


class ReplayTools:
    """Duck-types ToolRegistry: same schemas, recorded outcomes looked up by tool_use_id."""

    def __init__(self, schemas: list[dict[str, Any]], results: dict[str, dict[str, Any]]):
        self._schemas = schemas
        self._results = results

    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def execute(self, name: str, raw_input: Any, *, step: int) -> ToolOutcome:
        # execute() doesn't receive the tool_use_id, so match on (step, name, input) in recorded order.
        key = _call_key(step, name, raw_input)
        queue = self._results.get(key)
        if not queue:
            raise ReplayDivergence(f"step {step}: loop executed {name}({raw_input}), which the trace never executed")
        r = queue.pop(0)
        return ToolOutcome(content=r["content"], is_error=r["is_error"], error_kind=r.get("error_kind"),
                           attempts=r.get("attempts", 1), duration_ms=r.get("duration_ms", 0))


@dataclass
class ReplayReport:
    ok: bool
    message: str
    original_status: str
    replayed_status: str | None
    tool_calls: int


def replay(trace_path: Path) -> ReplayReport:
    events = read_trace(trace_path)
    start = next(e for e in events if e["type"] == "run_start")
    end = next((e for e in events if e["type"] == "run_end"), None)
    if end is None:
        return ReplayReport(False, "trace has no run_end event (the run crashed or is still running)", "?", None, 0)

    responses = [e["response"] for e in events if e["type"] == "model_response"]
    executed: dict[str, list[dict[str, Any]]] = {}
    calls = {e["tool_use_id"]: e for e in events if e["type"] == "tool_call"}
    for e in events:
        if e["type"] == "tool_result" and e.get("error_kind") != "guard_blocked":
            call = calls[e["tool_use_id"]]
            executed.setdefault(_call_key(e["step"], e["name"], call["input"]), []).append(e)

    tracer = Tracer(run_id=start["run_id"] + "-replay", path=None)
    agent = Agent(
        ReplayLLM(start.get("provider", "anthropic"), start["model"], responses), ReplayTools(start["tools"], executed),  # type: ignore[arg-type]
        tracer, config_from_dict(start["config"]), sleep=lambda _s: None,
    )
    try:
        result = agent.run(start["question"])
    except ReplayDivergence as exc:
        return ReplayReport(False, str(exc), end["status"], None, 0)

    original_calls = [(e["step"], e["name"], _canon(e["input"])) for e in events if e["type"] == "tool_call"]
    replayed_calls = [(e["step"], e["name"], _canon(e["input"])) for e in tracer.events if e["type"] == "tool_call"]
    for i, (a, b) in enumerate(zip(original_calls, replayed_calls)):
        if a != b:
            return ReplayReport(False, f"tool call #{i + 1} differs: recorded {a[1]} at step {a[0]}, "
                                f"replayed {b[1]} at step {b[0]}", end["status"], result.status, len(replayed_calls))
    if len(original_calls) != len(replayed_calls):
        return ReplayReport(False, f"recorded {len(original_calls)} tool calls, replay made {len(replayed_calls)}",
                            end["status"], result.status, len(replayed_calls))
    if result.status != end["status"]:
        return ReplayReport(False, f"status differs: recorded {end['status']}, replayed {result.status}",
                            end["status"], result.status, len(replayed_calls))
    if result.answer != end["answer"]:
        return ReplayReport(False, "final answer text differs", end["status"], result.status, len(replayed_calls))
    return ReplayReport(True, f"replay matched: {len(replayed_calls)} tool calls, {result.steps} steps, "
                        f"status {result.status}", end["status"], result.status, len(replayed_calls))


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _call_key(step: int, name: str, tool_input: Any) -> str:
    return f"{step}:{name}:{_canon(tool_input)}"
