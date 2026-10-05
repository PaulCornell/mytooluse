"""FastAPI server for the local web app (PRD 007).

The agent runs in a background thread. Its tracer already fans every event out to listeners;
here a listener stores each event and pushes it to any browser watching that run over
Server-Sent Events. The browser renders the events; this file contains no agent logic.

Security model (WEB-6..WEB-8): the agent can execute Python, so the server is for localhost only.
It binds to 127.0.0.1 by default, rejects unexpected Host headers (DNS rebinding), and rejects
cross-origin or non-JSON POSTs (so another website can't start a run from your browser).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.app import RUNS_DIR, run_question
from agent.budget import Budget
from agent.llm import DEFAULT_MODELS, LLM, LocalModelError, OllamaLLM, make_llm
from agent.tools import WebSearchTool
from agent.trace import new_run_id

STATIC_DIR = Path(__file__).parent / "static"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KEEPALIVE_S = 15.0
MAX_RUNS_IN_MEMORY = 20

EXAMPLES = [
    "Which model year has the most registered EVs?",
    "How many EVs are registered in King County, and what share are Teslas?",
    "What are the five most common EV makes?",
    "Which Washington city outside King County has the most registered EVs?",
    "What is the average electric range of model-year 2020 battery-electric vehicles?",
]

LLMFactory = Callable[[str, str | None], LLM]
StatusFn = Callable[[str, str | None], list[dict[str, Any]]]  # (default provider, default model) -> statuses


class RunRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    provider: str | None = None
    model: str | None = Field(default=None, max_length=100)
    max_steps: int = Field(default=12, ge=1, le=30)
    max_time_s: float = Field(default=300.0, ge=10, le=1800)
    chaos: float = Field(default=0.0, ge=0.0, le=0.9)
    seed: int | None = None
    prefetch_schema: bool | None = None


@dataclass
class RunState:
    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, event: dict[str, Any] | None) -> None:
        """Called from the agent's thread. None means the run is over."""
        with self.lock:
            if event is None:
                self.done = True
            else:
                self.events.append(event)
            subscribers = list(self.subscribers)
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(queue.put_nowait, event)


def default_llm_factory(provider: str, model: str | None) -> LLM:
    llm = make_llm(provider, model)
    if isinstance(llm, OllamaLLM):
        llm.preflight()  # raises LocalModelError with a fix-it message
    return llm


def create_app(
    *,
    db_path: Path,
    default_provider: str,
    default_model: str | None = None,
    runs_dir: Path = RUNS_DIR,
    llm_factory: LLMFactory = default_llm_factory,
    include_search: bool | None = None,
    allowed_hosts: list[str] | None = None,
    provider_status: StatusFn | None = None,
) -> FastAPI:
    app = FastAPI(title="Ask the Data", docs_url=None, redoc_url=None)
    hosts = allowed_hosts or ["127.0.0.1", "localhost"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    runs: dict[str, RunState] = {}
    active: dict[str, str | None] = {"run_id": None}
    guard = threading.Lock()

    @app.middleware("http")
    async def same_origin_json_posts(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).hostname not in hosts:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "default_provider": default_provider,
            "providers": (provider_status or default_provider_status)(default_provider, default_model),
            "database": _db_status(db_path),
            "search": WebSearchTool.available() if include_search is None else include_search,
            "examples": EXAMPLES,
            "busy": active["run_id"] is not None,
        }

    @app.post("/api/runs")
    def start_run(req: RunRequest) -> dict[str, str]:
        provider = req.provider or default_provider
        if provider not in DEFAULT_MODELS:
            raise HTTPException(400, f"unknown provider {provider!r}")
        if not db_path.exists():
            raise HTTPException(400, f"Database not found at {db_path}. Run `askdata load-data` first.")
        model = req.model or (default_model if provider == default_provider else None)
        busy = HTTPException(409, "A run is already in progress. Wait for it to finish.")
        if active["run_id"] is not None:  # WEB-4: one run at a time; refuse before any preflight work
            raise busy
        try:
            llm = llm_factory(provider, model)
        except LocalModelError as exc:
            raise HTTPException(400, str(exc)) from exc

        with guard:  # re-check: another request may have started while the model client was built
            if active["run_id"] is not None:
                raise busy
            run_id = new_run_id()
            state = RunState(run_id)
            runs[run_id] = state
            active["run_id"] = run_id
            for old in list(runs)[:-MAX_RUNS_IN_MEMORY]:
                runs.pop(old, None)

        def worker() -> None:
            try:
                run_question(
                    req.question, db_path=db_path, llm=llm,
                    budget=Budget(max_steps=req.max_steps, max_wall_s=req.max_time_s),
                    chaos=req.chaos, seed=req.seed, include_search=include_search,
                    listeners=[state.publish], prefetch_schema=req.prefetch_schema,
                    run_id=run_id, runs_dir=runs_dir,
                )
            except Exception as exc:  # surface crashes in the page instead of losing them
                last_seq = state.events[-1]["seq"] if state.events else 0
                state.publish({"run_id": run_id, "seq": last_seq + 1, "step": 0, "type": "server_error",
                               "message": f"{type(exc).__name__}: {exc}"})
            finally:
                with guard:
                    active["run_id"] = None
                state.publish(None)

        threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
        return {"run_id": run_id}

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return _list_runs(runs_dir)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request) -> StreamingResponse:
        if not _RUN_ID.match(run_id):
            raise HTTPException(404, "no such run")
        after = int(request.headers.get("last-event-id") or 0)
        state = runs.get(run_id)
        if state is None:
            path = runs_dir / f"{run_id}.jsonl"
            if not path.exists():
                raise HTTPException(404, "no such run")
            state = RunState(run_id, events=_read_events(path), done=True)
        return StreamingResponse(_stream(state, after), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


async def _stream(state: RunState, after: int) -> AsyncIterator[str]:
    """Send the backlog, then live events, then an `end` event. Reconnects resume via Last-Event-ID."""
    queue: asyncio.Queue = asyncio.Queue()
    subscriber = (asyncio.get_running_loop(), queue)
    with state.lock:  # snapshot + subscribe atomically, so no event is missed or duplicated
        backlog = [e for e in state.events if e["seq"] > after]
        done = state.done
        if not done:
            state.subscribers.append(subscriber)
    try:
        for event in backlog:
            yield _sse(event)
        if done:
            yield "event: end\ndata: {}\n\n"
            return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                yield "event: end\ndata: {}\n\n"
                return
            yield _sse(event)
    finally:
        with state.lock:
            if subscriber in state.subscribers:
                state.subscribers.remove(subscriber)


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"


def _read_events(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _list_runs(runs_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for path in sorted(runs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            start, last = json.loads(lines[0]), json.loads(lines[-1])
        except (OSError, IndexError, json.JSONDecodeError):
            continue
        out.append({
            "run_id": path.stem, "question": start.get("question", ""), "ts": start.get("ts"),
            "provider": start.get("provider", "anthropic"), "model": start.get("model"),
            "status": last.get("status") if last.get("type") == "run_end" else "running",
        })
    return out


def default_provider_status(default_provider: str, default_model: str | None) -> list[dict[str, Any]]:
    return [_ollama_status(default_model if default_provider == "ollama" else None),
            _anthropic_status(default_model if default_provider == "anthropic" else None)]


def _ollama_status(model: str | None) -> dict[str, Any]:
    llm = OllamaLLM(model or DEFAULT_MODELS["ollama"])
    status = {"id": "ollama", "label": "Local (Ollama) · free", "model": llm.model}
    try:
        warnings = llm.preflight()
        return {**status, "available": True, "warnings": warnings, "message": None}
    except LocalModelError as exc:
        return {**status, "available": False, "warnings": [], "message": str(exc)}


def _anthropic_status(model: str | None) -> dict[str, Any]:
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return {"id": "anthropic", "label": "Claude · paid", "model": model or DEFAULT_MODELS["anthropic"],
            "available": has_key, "warnings": [],
            "message": None if has_key else "Set ANTHROPIC_API_KEY before starting the server to use Claude (billed per token)."}


def _db_status(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"ok": False, "path": str(db_path), "rows": 0,
                "message": "Database not found. Run `askdata load-data`, then reload this page."}
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        (rows,) = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()
        conn.close()
        return {"ok": True, "path": str(db_path), "rows": rows, "message": None}
    except sqlite3.Error as exc:
        return {"ok": False, "path": str(db_path), "rows": 0, "message": f"Database error: {exc}"}
