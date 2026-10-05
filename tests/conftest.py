from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from agent.data import load
from agent.tools import ToolContext

FIXTURES = Path(__file__).parent / "fixtures"

# Browser tests need the optional e2e extra (pip install -e '.[e2e]'); without it, don't collect them.
try:
    import playwright  # noqa: F401
except ImportError:
    collect_ignore = ["e2e"]
_ids = itertools.count(1)


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "ev_sample.db"
    load(FIXTURES / "ev_sample.csv", path)
    return path


@pytest.fixture
def ctx(db_path: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(db_path=db_path, workdir=tmp_path / "work")


def message(*blocks: dict[str, Any], stop_reason: str = "end_turn", input_tokens: int = 1000,
            output_tokens: int = 100, **extra: Any) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": list(blocks), "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}, **extra,
    })


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool_use(name: str, tool_input: dict[str, Any], id: str | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": id or f"toolu_{next(_ids)}", "name": name, "input": tool_input}


class ScriptedLLM:
    """Returns pre-baked responses in order; records every request for assertions.

    A script entry may be an Exception instance, which is raised instead of returning.
    """

    provider = "anthropic"

    def __init__(self, script: list[BetaMessage | Exception], model: str = "claude-sonnet-5-5",
                 context_window: int | None = None):
        self.model = model
        self.context_window = context_window
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create(self, *, system, messages, tools, tool_choice=None, timeout=None) -> BetaMessage:
        self.requests.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice,
                              "n_messages": len(messages), "timeout": timeout})
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
