import json
from pathlib import Path

import pytest

from agent.loop import Agent, AgentConfig
from agent.replay import replay
from agent.tools import GetSchemaTool, RunPythonTool, RunSqlTool, ToolRegistry
from agent.trace import Tracer
from tests.conftest import FIXTURES, ScriptedLLM, message, text, tool_use


@pytest.fixture
def recorded(ctx, tmp_path) -> Path:
    path = tmp_path / "run.jsonl"
    with Tracer("rec", path) as tracer:
        reg = ToolRegistry([GetSchemaTool(), RunSqlTool(), RunPythonTool()], ctx, tracer=tracer)
        llm = ScriptedLLM([
            message(tool_use("get_schema", {}), stop_reason="tool_use"),
            message(tool_use("run_sql", {"sql": "SELECT bad FROM vehicles"}), stop_reason="tool_use"),
            message(tool_use("run_sql", {"sql": "SELECT COUNT(*) FROM vehicles"}),
                    tool_use("run_python", {"code": "print(500 / 2)"}), stop_reason="tool_use"),
            message(text("Half of 500 is 250.")),
        ])
        Agent(llm, reg, tracer).run("What is half the row count?")
    return path


def test_replay_matches(recorded):
    report = replay(recorded)
    assert report.ok, report.message
    assert report.tool_calls == 4 and report.replayed_status == "completed"


def test_replay_detects_divergence(recorded, tmp_path):
    # Simulate a behavior change: the recorded run executed a call the replayed loop won't ask for.
    events = [json.loads(line) for line in recorded.read_text().splitlines()]
    for e in events:
        if e["type"] == "tool_call" and e["name"] == "run_python":
            e["input"] = {"code": "print('different')"}
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    report = replay(tampered)
    assert not report.ok and "run_python" in report.message


def test_replay_rejects_incomplete_trace(recorded, tmp_path):
    lines = recorded.read_text().splitlines()
    partial = tmp_path / "partial.jsonl"
    partial.write_text("\n".join(lines[:-1]) + "\n")
    assert not replay(partial).ok


def test_committed_fixture_trace_replays():
    fixture = FIXTURES / "sample_trace.jsonl"
    if not fixture.exists():
        pytest.skip("no recorded fixture trace")
    report = replay(fixture)
    assert report.ok, report.message


def test_replay_with_prefetched_schema(ctx, tmp_path):
    path = tmp_path / "prefetch.jsonl"
    with Tracer("pf", path) as tracer:
        reg = ToolRegistry([GetSchemaTool(), RunSqlTool()], ctx, tracer=tracer)
        llm = ScriptedLLM([
            message(tool_use("run_sql", {"sql": "SELECT COUNT(*) FROM vehicles"}), stop_reason="tool_use"),
            message(text("500")),
        ])
        Agent(llm, reg, tracer, AgentConfig(prefetch_schema=True)).run("How many?")
    report = replay(path)
    assert report.ok, report.message
    assert report.tool_calls == 2
