import anthropic
import httpx2

from agent.budget import Budget
from agent.chaos import Chaos
from agent.loop import FINAL_STEP_NOTICE, Agent, AgentConfig
from agent.retry import RetryPolicy
from agent.tools import GetSchemaTool, RunPythonTool, RunSqlTool, ToolRegistry
from agent.trace import Tracer
from tests.conftest import ScriptedLLM, message, text, tool_use


def make_agent(ctx, script, *, config=None, chaos=None, tmp_path=None):
    tracer = Tracer("test-run", tmp_path / "trace.jsonl" if tmp_path else None)
    reg = ToolRegistry([GetSchemaTool(), RunSqlTool(), RunPythonTool()], ctx, tracer=tracer,
                       chaos=chaos, sleep=lambda s: None)
    llm = ScriptedLLM(script)
    return Agent(llm, reg, tracer, config or AgentConfig(), sleep=lambda s: None), llm, tracer


def events(tracer, type_):
    return [e for e in tracer.events if e["type"] == type_]


def test_recovers_from_bad_sql(ctx):
    agent, llm, tracer = make_agent(ctx, [
        message(tool_use("run_sql", {"sql": "SELECT manufacturer FROM vehicles"}), stop_reason="tool_use"),
        message(tool_use("run_sql", {"sql": "SELECT COUNT(DISTINCT make) FROM vehicles"}), stop_reason="tool_use"),
        message(text("There are N makes.")),
    ])
    result = agent.run("How many makes?")

    assert result.status == "completed" and result.answer == "There are N makes." and result.steps == 3
    first, second = events(tracer, "tool_result")
    assert first["is_error"] and "no such column" in first["content"]
    assert not second["is_error"]
    # the error went back to the model as an is_error tool_result
    sent = llm.requests[1]["messages"][-1]["content"][0]
    assert sent["is_error"] is True and sent["tool_use_id"] == first["tool_use_id"]


def test_parallel_calls_return_one_message_in_order(ctx):
    agent, llm, _ = make_agent(ctx, [
        message(tool_use("run_sql", {"sql": "SELECT 1 AS a"}, id="t_a"),
                tool_use("run_python", {"code": "print(2)"}, id="t_b"), stop_reason="tool_use"),
        message(text("done")),
    ])
    agent.run("q")
    last = llm.requests[1]["messages"][-1]
    assert last["role"] == "user"
    assert [b["tool_use_id"] for b in last["content"]] == ["t_a", "t_b"]
    assert llm.requests[1]["n_messages"] == 3  # user, assistant, user(tool_results)


def test_history_is_append_only(ctx):
    agent, llm, _ = make_agent(ctx, [
        message({"type": "thinking", "thinking": "plan", "signature": "sig"},
                tool_use("get_schema", {}), stop_reason="tool_use"),
        message(text("ok")),
    ])
    agent.run("q")
    assistant = llm.requests[1]["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0].type == "thinking" and assistant["content"][0].signature == "sig"


def test_refusal_ends_run(ctx):
    agent, _, _ = make_agent(ctx, [message(stop_reason="refusal", stop_details={
        "type": "refusal", "category": "cyber", "explanation": None})])
    result = agent.run("q")
    assert result.status == "refused" and "cyber" in result.detail


def test_max_tokens_is_truncated(ctx):
    agent, _, _ = make_agent(ctx, [message(text("partial"), stop_reason="max_tokens")])
    result = agent.run("q")
    assert result.status == "truncated" and result.answer == "partial"


def test_final_step_forces_an_answer(ctx):
    config = AgentConfig(budget=Budget(max_steps=2))
    agent, llm, tracer = make_agent(ctx, [
        message(tool_use("get_schema", {}), stop_reason="tool_use"),
        message(text("best effort answer")),
    ], config=config)
    result = agent.run("q")
    assert result.status == "completed"
    assert llm.requests[1]["tool_choice"] == {"type": "none"}
    assert llm.requests[1]["messages"][-1]["content"][-1] == {"type": "text", "text": FINAL_STEP_NOTICE}
    assert events(tracer, "guard")[0]["reason"] == "final_step"


def test_step_budget_stops_run(ctx):
    config = AgentConfig(budget=Budget(max_steps=1))
    agent, _, _ = make_agent(ctx, [message(tool_use("get_schema", {}), stop_reason="tool_use")], config=config)
    result = agent.run("q")
    assert result.status == "budget_exceeded" and "max_steps" in result.detail


def test_cost_budget_stops_run(ctx):
    config = AgentConfig(budget=Budget(max_cost_usd=0.001))
    agent, _, _ = make_agent(ctx, [message(tool_use("get_schema", {}), stop_reason="tool_use", input_tokens=10_000)],
                             config=config)
    result = agent.run("q")
    assert result.status == "budget_exceeded" and "max_cost_usd" in result.detail


def test_repeated_call_is_blocked_then_aborted(ctx):
    bad = {"sql": "SELECT nope FROM vehicles"}
    script = [message(tool_use("run_sql", bad), stop_reason="tool_use") for _ in range(5)]
    agent, _, tracer = make_agent(ctx, script)
    result = agent.run("q")
    kinds = [e["error_kind"] for e in events(tracer, "tool_result")]
    assert kinds == ["execution_error", "execution_error", "guard_blocked", "guard_blocked"]
    assert result.status == "aborted"


def test_transient_api_errors_are_retried(ctx):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.InternalServerError("overloaded", response=httpx2.Response(529, request=request), body=None)
    agent, _, tracer = make_agent(ctx, [overloaded, overloaded, message(text("fine"))])
    result = agent.run("q")
    assert result.status == "completed"
    assert [e["target"] for e in events(tracer, "retry")] == ["model", "model"]
    assert [e["attempt"] for e in events(tracer, "model_request")] == [1, 2, 3]


def test_auth_error_is_fatal(ctx):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    denied = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=request), body=None)
    agent, _, tracer = make_agent(ctx, [denied])
    result = agent.run("q")
    assert result.status == "error" and "AuthenticationError" in result.detail
    assert events(tracer, "retry") == []


def test_completes_under_chaos(ctx):
    script = [message(tool_use("run_sql", {"sql": f"SELECT {i}"}), stop_reason="tool_use") for i in range(4)]
    agent, _, tracer = make_agent(ctx, [*script, message(text("survived"))], chaos=Chaos(0.5, seed=11))
    result = agent.run("q")
    assert result.status == "completed"
    assert any(e["injected"] for e in events(tracer, "retry"))


def test_trace_is_written_and_complete(ctx, tmp_path):
    agent, _, tracer = make_agent(ctx, [
        message(tool_use("get_schema", {}), stop_reason="tool_use"), message(text("a")),
    ], tmp_path=tmp_path)
    agent.run("q")
    tracer.close()
    lines = (tmp_path / "trace.jsonl").read_text().splitlines()
    types = [__import__("json").loads(line)["type"] for line in lines]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert {"model_request", "model_response", "tool_call", "tool_result"} <= set(types)
    assert [__import__("json").loads(line)["seq"] for line in lines] == list(range(1, len(lines) + 1))


def test_retry_policy_from_config_is_used(ctx):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError("boom", response=httpx2.Response(500, request=request), body=None)
    config = AgentConfig(api_retry=RetryPolicy(max_attempts=2))
    agent, _, _ = make_agent(ctx, [err, err, message(text("never"))], config=config)
    assert agent.run("q").status == "error"


def test_prefetch_schema_puts_schema_in_first_message(ctx):
    agent, llm, tracer = make_agent(ctx, [message(text("answer"))], config=AgentConfig(prefetch_schema=True))
    agent.run("How many?")
    first = llm.requests[0]["messages"][0]["content"]
    assert first.startswith("<database_schema>") and "## vehicles" in first
    assert first.endswith("<question>How many?</question>")
    calls = events(tracer, "tool_call")
    assert [(c["step"], c["name"], c["tool_use_id"]) for c in calls] == [(0, "get_schema", "prefetch")]


def test_prefetch_is_off_by_default(ctx):
    agent, llm, _ = make_agent(ctx, [message(text("answer"))])
    agent.run("How many?")
    assert llm.requests[0]["messages"][0]["content"] == "How many?"


def test_model_calls_get_only_the_remaining_wall_clock(ctx):
    now = [0.0]
    request = httpx2.Request("POST", "http://localhost:11434/v1/messages")

    class SlowLLM(ScriptedLLM):
        def create(self, **kw):
            now[0] += 70.0  # each call (successful or not) burns 70s of wall clock
            return super().create(**kw)

    timeout = anthropic.APITimeoutError(request=request)
    llm = SlowLLM([message(tool_use("get_schema", {}), stop_reason="tool_use"), timeout, timeout, timeout])
    tracer = Tracer("t")
    reg = ToolRegistry([GetSchemaTool()], ctx, tracer=tracer, sleep=lambda s: None)
    agent = Agent(llm, reg, tracer, AgentConfig(budget=Budget(max_wall_s=180)), clock=lambda: now[0],
                  sleep=lambda s: None)
    result = agent.run("q")

    assert [r["timeout"] for r in llm.requests] == [180.0, 110.0, 40.0]  # shrinks with elapsed time
    assert result.status == "budget_exceeded" and "during model call" in result.detail
