"""Wire the pieces together: one function the CLI and the eval runner both use."""

from __future__ import annotations

from pathlib import Path

from agent.budget import Budget
from agent.chaos import Chaos
from agent.llm import LLM
from agent.loop import Agent, AgentConfig, RunResult
from agent.tools import ToolContext, ToolRegistry, default_tools
from agent.trace import Listener, Tracer, new_run_id

RUNS_DIR = Path("runs")


def run_question(
    question: str,
    *,
    db_path: Path,
    llm: LLM,
    budget: Budget | None = None,
    chaos: float = 0.0,
    seed: int | None = None,
    trace_path: Path | None = None,
    include_search: bool | None = None,
    listeners: list[Listener] | None = None,
    prefetch_schema: bool | None = None,
    run_id: str | None = None,
    runs_dir: Path = RUNS_DIR,
) -> RunResult:
    run_id = run_id or new_run_id()
    trace_path = trace_path or runs_dir / f"{run_id}.jsonl"
    with Tracer(run_id, trace_path, listeners) as tracer:
        ctx = ToolContext(db_path=db_path, workdir=runs_dir / run_id / "work")
        registry = ToolRegistry(
            default_tools(include_search=include_search), ctx, tracer=tracer,
            chaos=Chaos(chaos, seed) if chaos > 0 else None,
        )
        if prefetch_schema is None:
            prefetch_schema = llm.provider == "ollama"  # LOCAL-10
        config = AgentConfig(budget=budget or Budget(), prefetch_schema=prefetch_schema)
        agent = Agent(llm, registry, tracer, config)
        return agent.run(question)
