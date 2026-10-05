"""Tool contract and registry (PRD 002 §2).

The registry is the single place where a model-issued tool call becomes a result:
look up -> validate -> (chaos) -> run -> retry transient failures -> classify the outcome.
It never raises: every call produces a ToolOutcome, because every tool_use needs a
tool_result (LOOP-4).
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ValidationError

from agent.chaos import Chaos
from agent.errors import ModelFixableError, ToolInputError, TransientError
from agent.retry import RetryPolicy, call_with_retry, classify_tool_error
from agent.trace import Tracer


@dataclass(frozen=True)
class ToolContext:
    db_path: Path
    workdir: Path  # per-run scratch directory (run_python's cwd)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Input: ClassVar[type[BaseModel]]
    timeout_s: ClassVar[float] = 30.0

    @abstractmethod
    def run(self, args: BaseModel, ctx: ToolContext) -> str: ...

    def api_schema(self) -> dict[str, Any]:
        schema = self.Input.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    error_kind: str | None = None
    attempts: int = 1
    duration_ms: int = 0

    def to_block(self, tool_use_id: str) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": self.content}
        if self.is_error:
            block["is_error"] = True
        return block


class ToolRegistry:
    def __init__(
        self,
        tools: list[Tool],
        ctx: ToolContext,
        *,
        tracer: Tracer,
        policy: RetryPolicy = RetryPolicy(max_attempts=3, base_delay_s=0.5, max_delay_s=8.0),
        chaos: Chaos | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.tools = {t.name: t for t in tools}
        self.ctx = ctx
        self.tracer = tracer
        self.policy = policy
        self.chaos = chaos
        self.sleep = sleep
        self.rng = rng or random.Random()
        self._schemas = [t.api_schema() for t in tools]  # built once: byte-stable for caching (LOOP-9)

    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def execute(self, name: str, raw_input: Any, *, step: int) -> ToolOutcome:
        started = time.monotonic()
        outcome = self._execute(name, raw_input, step)
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _execute(self, name: str, raw_input: Any, step: int) -> ToolOutcome:
        tool = self.tools.get(name)
        if tool is None:
            return _error(ToolInputError(f"Unknown tool '{name}'. Available tools: {', '.join(self.tools)}."))

        try:
            args = tool.Input.model_validate(raw_input)
        except ValidationError as exc:
            return _error(ToolInputError(f"Invalid input for {name}: {_format_validation(exc)}"))

        attempts = 0

        def attempt() -> str:
            nonlocal attempts
            attempts += 1
            if self.chaos:
                self.chaos.maybe_fail(name)
            return tool.run(args, self.ctx)

        def on_retry(attempt_no: int, delay: float, exc: Exception) -> None:
            self.tracer.emit(
                "retry", step, target=name, attempt=attempt_no, delay_s=round(delay, 3),
                error=str(exc), injected=getattr(exc, "injected", False),
            )

        try:
            content, _ = call_with_retry(
                attempt, policy=self.policy, classify=classify_tool_error,
                on_retry=on_retry, sleep=self.sleep, rng=self.rng,
            )
            return ToolOutcome(content=content, attempts=attempts)
        except ModelFixableError as exc:
            outcome = _error(exc)
        except TransientError as exc:
            outcome = ToolOutcome(
                content=f"{name} is temporarily unavailable after {attempts} attempts "
                f"({exc}). Try a different approach or answer with the information you have.",
                is_error=True, error_kind="transient_exhausted",
            )
        except Exception as exc:  # a bug in a tool must not crash the run
            outcome = ToolOutcome(
                content=f"{name} failed with an internal error: {type(exc).__name__}: {exc}",
                is_error=True, error_kind="internal",
            )
        outcome.attempts = attempts
        return outcome


def _error(exc: ModelFixableError) -> ToolOutcome:
    return ToolOutcome(content=str(exc), is_error=True, error_kind=exc.kind)


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(input)"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
