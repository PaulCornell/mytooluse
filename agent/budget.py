"""Budgets, cost accounting and the repeated-call guard (PRD 003, REL-8..REL-13)."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# USD per million tokens: (input, output, cache_read). Cache writes bill at 1.25x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def from_api(cls, usage: Any) -> "Usage":
        return cls(
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(*(a + b for a, b in zip(asdict(self).values(), asdict(other).values())))

    @property
    def total_tokens(self) -> int:
        return sum(asdict(self).values())

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens

    def cost_usd(self, model: str) -> float:
        if not model.startswith("claude-"):
            return 0.0  # local models (LOCAL-5)
        # An unlisted Claude model is priced as Opus so that the cost budget errs on the safe side.
        input_price, output_price, cache_read_price = PRICES.get(model, PRICES["claude-opus-5-5"])
        return (
            self.input_tokens * input_price
            + self.output_tokens * output_price
            + self.cache_write_tokens * input_price * CACHE_WRITE_MULTIPLIER
            + self.cache_read_tokens * cache_read_price
        ) / 1_000_000


@dataclass(frozen=True)
class Budget:
    max_steps: int = 12
    max_tokens: int = 400_000
    max_cost_usd: float = 0.50
    max_wall_s: float = 300.0


@dataclass
class BudgetTracker:
    budget: Budget
    model: str
    clock: Callable[[], float] = time.monotonic
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        self.started = self.clock()

    def record(self, usage: Usage) -> float:
        """Add one model call's usage. Returns that call's cost."""
        cost = usage.cost_usd(self.model)
        self.usage = self.usage + usage
        self.cost_usd += cost
        return cost

    @property
    def elapsed_s(self) -> float:
        return self.clock() - self.started

    def exceeded(self) -> str | None:
        """Name the first budget that is exhausted, or None. Checked before each model call."""
        b = self.budget
        if self.steps >= b.max_steps:
            return f"max_steps ({b.max_steps})"
        if self.usage.total_tokens >= b.max_tokens:
            return f"max_tokens ({b.max_tokens:,})"
        if self.cost_usd >= b.max_cost_usd:
            return f"max_cost_usd (${b.max_cost_usd:.2f})"
        if self.elapsed_s >= b.max_wall_s:
            return f"max_wall_s ({b.max_wall_s:.0f}s)"
        return None


@dataclass
class CallGuard:
    """Stops the model from repeating the exact same tool call (REL-12, REL-13)."""

    block_at: int = 3
    abort_at: int = 5
    counts: Counter = field(default_factory=Counter)

    def check(self, name: str, tool_input: Any) -> str:
        """Count this call. Returns 'ok', 'block' or 'abort'."""
        key = name + ":" + json.dumps(tool_input, sort_keys=True, default=str)
        self.counts[key] += 1
        n = self.counts[key]
        if n >= self.abort_at:
            return "abort"
        if n >= self.block_at:
            return "block"
        return "ok"
