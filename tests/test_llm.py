import json

import anthropic
import httpx
import httpx2
import pytest

from agent.budget import Usage
from agent.llm import AnthropicLLM, LocalModelError, OllamaLLM, make_llm
from agent.loop import Agent
from agent.tools import GetSchemaTool, ToolRegistry
from agent.trace import Tracer
from tests.conftest import ScriptedLLM, message, text, tool_use


def capture_client(**client_kwargs):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=message(text("hi")).to_dict())

    client = anthropic.Anthropic(api_key="k", max_retries=0, **client_kwargs,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return client, seen


def call(llm):
    llm.create(system="s", messages=[{"role": "user", "content": "q"}], tools=[GetSchemaTool().api_schema()])


def test_ollama_sends_only_portable_parameters():
    client, seen = capture_client(base_url="http://localhost:11434")
    call(OllamaLLM("askdata-local", client=client))
    assert seen["url"].startswith("http://localhost:11434/v1/messages")
    assert set(seen["body"]) == {"model", "max_tokens", "system", "messages", "tools"}
    assert "anthropic-beta" not in seen["headers"]


def test_claude_sends_thinking_effort_caching_and_fallback():
    client, seen = capture_client()
    call(AnthropicLLM("claude-sonnet-5-5", client=client))
    body = seen["body"]
    assert body["thinking"]["type"] == "adaptive" and body["output_config"] == {"effort": "medium"}
    assert body["cache_control"] == {"type": "ephemeral"} and body["fallbacks"] == "default"
    assert seen["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"


def test_make_llm_defaults():
    assert make_llm("ollama").model == "askdata-local"
    assert make_llm("anthropic").model == "claude-sonnet-5-5"
    with pytest.raises(ValueError):
        make_llm("openai")


# --- preflight -----------------------------------------------------------------------------


def ollama_with(handler):
    return OllamaLLM("askdata-local", http=httpx.Client(transport=httpx.MockTransport(handler)))


def show(capabilities=("completion", "tools"), parameters="num_ctx                        16384"):
    return lambda req: httpx.Response(200, json={"capabilities": list(capabilities), "parameters": parameters})


def test_preflight_ok_reads_context_window():
    llm = ollama_with(show())
    assert llm.preflight() == [] and llm.context_window == 16384


def test_preflight_ollama_not_running():
    def refuse(req):
        raise httpx.ConnectError("connection refused")
    with pytest.raises(LocalModelError, match="ollama serve"):
        ollama_with(refuse).preflight()


def test_preflight_model_missing_suggests_setup():
    with pytest.raises(LocalModelError, match="askdata setup-local"):
        ollama_with(lambda req: httpx.Response(404, json={"error": "not found"})).preflight()


def test_preflight_rejects_model_without_tools():
    with pytest.raises(LocalModelError, match="tool calling"):
        ollama_with(show(capabilities=("completion",))).preflight()


def test_preflight_warns_about_default_context():
    warnings = ollama_with(show(parameters="")).preflight()
    assert len(warnings) == 1 and "4,096" in warnings[0]


# --- cost and context guard ----------------------------------------------------------------


def test_local_models_cost_nothing():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert u.cost_usd("askdata-local") == 0.0 and u.cost_usd("qwen2.5:3b") == 0.0
    assert u.cost_usd("claude-some-future-model") > 0  # unlisted Claude models are priced conservatively


def test_loop_warns_when_prompt_nears_context_window(ctx):
    tracer = Tracer("t")
    llm = ScriptedLLM([
        message(tool_use("get_schema", {}), stop_reason="tool_use", input_tokens=500),
        message(text("done"), input_tokens=3900),
    ], model="askdata-local", context_window=4096)
    Agent(llm, ToolRegistry([GetSchemaTool()], ctx, tracer=tracer), tracer).run("q")
    guards = [e for e in tracer.events if e["type"] == "guard"]
    assert [g["reason"] for g in guards] == ["context_near_limit"] and guards[0]["step"] == 2
