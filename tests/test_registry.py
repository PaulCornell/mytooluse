from pydantic import BaseModel

from agent.chaos import Chaos
from agent.errors import ToolExecutionError, TransientToolError
from agent.retry import RetryPolicy
from agent.tools import RunSqlTool, ToolRegistry
from agent.tools.base import Tool
from agent.trace import Tracer


class FlakyInput(BaseModel):
    n: int


class FlakyTool(Tool):
    """Fails transiently `failures` times, then succeeds."""

    name = "flaky"
    description = "test tool"
    Input = FlakyInput

    def __init__(self, failures: int = 0, error: Exception | None = None):
        self.failures, self.error, self.calls = failures, error, 0

    def run(self, args, ctx):
        self.calls += 1
        if self.error:
            raise self.error
        if self.calls <= self.failures:
            raise TransientToolError("503")
        return f"n={args.n}"


def registry(tools, ctx, **kw):
    tracer = Tracer("test")
    return ToolRegistry(tools, ctx, tracer=tracer, sleep=lambda s: None,
                        policy=RetryPolicy(max_attempts=3), **kw), tracer


def test_invalid_input_is_returned_without_running(ctx):
    tool = FlakyTool()
    reg, _ = registry([tool], ctx)
    out = reg.execute("flaky", {"n": "not a number"}, step=1)
    assert out.is_error and out.error_kind == "invalid_input" and "n:" in out.content
    assert tool.calls == 0


def test_unknown_tool(ctx):
    reg, _ = registry([FlakyTool()], ctx)
    out = reg.execute("nope", {}, step=1)
    assert out.is_error and "Available tools: flaky" in out.content


def test_transient_failures_are_retried(ctx):
    reg, tracer = registry([FlakyTool(failures=2)], ctx)
    out = reg.execute("flaky", {"n": 1}, step=1)
    assert not out.is_error and out.content == "n=1" and out.attempts == 3
    assert [e["type"] for e in tracer.events] == ["retry", "retry"]


def test_transient_exhausted_tells_model(ctx):
    reg, _ = registry([FlakyTool(failures=99)], ctx)
    out = reg.execute("flaky", {"n": 1}, step=1)
    assert out.is_error and out.error_kind == "transient_exhausted" and "temporarily unavailable" in out.content


def test_fixable_error_is_not_retried(ctx):
    tool = FlakyTool(error=ToolExecutionError("bad"))
    reg, tracer = registry([tool], ctx)
    out = reg.execute("flaky", {"n": 1}, step=1)
    assert out.is_error and out.error_kind == "execution_error" and tool.calls == 1
    assert tracer.events == []


def test_tool_bug_does_not_crash(ctx):
    reg, _ = registry([FlakyTool(error=KeyError("oops"))], ctx)
    out = reg.execute("flaky", {"n": 1}, step=1)
    assert out.is_error and out.error_kind == "internal" and "KeyError" in out.content


def test_chaos_failures_are_marked_injected(ctx):
    reg, tracer = registry([RunSqlTool()], ctx, chaos=Chaos(0.5, seed=3))
    outcomes = [reg.execute("run_sql", {"sql": "SELECT 1"}, step=1) for _ in range(10)]
    retries = [e for e in tracer.events if e["type"] == "retry"]
    assert retries and all(e["injected"] for e in retries)
    assert sum(not o.is_error for o in outcomes) >= 7


def test_schemas_are_stable(ctx):
    reg, _ = registry([RunSqlTool()], ctx)
    assert reg.schemas() is reg.schemas()
    schema = reg.schemas()[0]
    assert schema["name"] == "run_sql"
    assert schema["input_schema"]["required"] == ["sql"]
    assert schema["input_schema"]["additionalProperties"] is False


def test_attempts_are_counted_when_a_retried_call_finally_fails(ctx):
    class FlakyThenBroken(FlakyTool):
        def run(self, args, ctx):
            self.calls += 1
            if self.calls <= 2:
                raise TransientToolError("503")
            raise ToolExecutionError("no such column")

    reg, _ = registry([FlakyThenBroken()], ctx)
    out = reg.execute("flaky", {"n": 1}, step=1)
    assert out.is_error and out.error_kind == "execution_error" and out.attempts == 3
