"""Command-line interface: askdata {ask, serve, view, replay, eval, setup-local, load-data}."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent.budget import Budget
from agent.data import DEFAULT_CSV, DEFAULT_DB
from agent.llm import DEFAULT_MODELS, DEFAULT_PROVIDER, PROVIDERS, LocalModelError, OllamaLLM, make_llm

LOCAL_BASE_MODEL = "qwen2.5:3b"
LOCAL_CONTEXT = 16_384


def _live(event: dict[str, Any]) -> None:
    """OBS-8: one line per event on stderr."""
    t, step = event["type"], event["step"]
    if t == "model_response":
        u = event["usage"]
        print(f"[{step}] model  stop={event['stop_reason']:<9} in={u['input_tokens']:,} "
              f"cache={u['cache_read_tokens']:,} out={u['output_tokens']:,} {event['duration_ms'] / 1000:.1f}s",
              file=sys.stderr)
    elif t == "tool_call" and event["tool_use_id"] == "prefetch":
        print("[0] prefetch get_schema (schema goes into the first message)", file=sys.stderr)
    elif t == "tool_call":
        arg = next(iter(event["input"].values()), "") if event["input"] else ""
        print(f"[{step}] call   {event['name']}  {str(arg).splitlines()[0][:90] if arg else ''}", file=sys.stderr)
    elif t == "tool_result":
        flag = f"ERROR ({event['error_kind']})" if event["is_error"] else "ok"
        first = event["content"].splitlines()[0][:80] if event["content"] else ""
        print(f"[{step}] result {event['name']}  {flag}  {event['duration_ms']}ms  {first}", file=sys.stderr)
    elif t == "retry":
        tag = " [chaos]" if event.get("injected") else ""
        print(f"[{step}] retry  {event['target']} #{event['attempt']} in {event['delay_s']:.2f}s{tag}: "
              f"{event['error'][:80]}", file=sys.stderr)
    elif t == "guard":
        print(f"[{step}] guard  {event['reason']}: {event['detail']}", file=sys.stderr)


def _budget(args: argparse.Namespace) -> Budget:
    return Budget(max_steps=args.max_steps, max_cost_usd=args.max_cost, max_wall_s=args.max_time)


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database (default: %(default)s)")
    p.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS,
                   help="ollama = free local model; anthropic = Claude, needs ANTHROPIC_API_KEY (default: %(default)s)")
    p.add_argument("--model", help=f"model name (default: {DEFAULT_MODELS['ollama']} for ollama, "
                                   f"{DEFAULT_MODELS['anthropic']} for anthropic)")
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                   help="Claude only")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--max-cost", type=float, default=0.50, help="USD per question")
    p.add_argument("--max-time", type=float, default=300.0, help="seconds per question")
    p.add_argument("--chaos", type=float, default=0.0, help="probability of injecting a transient tool failure")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for --chaos")
    p.add_argument("--prefetch-schema", action=argparse.BooleanOptionalAction, default=None,
                   help="put the schema in the first message (default: on for ollama, off for anthropic)")


def _make_llm(args: argparse.Namespace):
    """Build the model client. For local models, fail fast with a fix-it message (LOCAL-3)."""
    llm = make_llm(args.provider, args.model, effort=args.effort)
    if isinstance(llm, OllamaLLM):
        try:
            for warning in llm.preflight():
                print(f"warning: {warning}", file=sys.stderr)
        except LocalModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None
    return llm


def setup_local(base: str, ctx: int, name: str) -> int:
    """Pull an open-weight model and create a variant with a usable context window (LOCAL-2)."""
    if not shutil.which("ollama"):
        print("error: the `ollama` command wasn't found. Install Ollama from https://ollama.com first.", file=sys.stderr)
        return 2
    print(f"Pulling {base} (skipped if already present)...", file=sys.stderr)
    if subprocess.run(["ollama", "pull", base]).returncode != 0:
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        modelfile = Path(tmp) / "Modelfile"
        modelfile.write_text(f"FROM {base}\nPARAMETER num_ctx {ctx}\n")
        print(f"Creating {name} from {base} with a {ctx:,}-token context...", file=sys.stderr)
        if subprocess.run(["ollama", "create", name, "-f", str(modelfile)]).returncode != 0:
            return 1
    try:
        warnings = OllamaLLM(name).preflight()
    except LocalModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Ready. Try: askdata ask \"How many EVs are registered in King County?\"", file=sys.stderr)
    return 0


def serve(args: argparse.Namespace) -> int:
    """Run the local web app (PRD 007)."""
    import threading
    import webbrowser

    import uvicorn

    from agent.web.server import create_app

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"warning: binding to {args.host} lets other machines start runs, and runs can execute Python. "
              "Only do this on a network you trust.", file=sys.stderr)
    app = create_app(db_path=args.db, default_provider=args.provider, default_model=args.model,
                     allowed_hosts=sorted({args.host, "127.0.0.1", "localhost"}))
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"Ask the Data is running at {url}  (Ctrl+C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askdata", description="A multi-step tool-using data agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="answer a question")
    p_ask.add_argument("question")
    _add_run_options(p_ask)
    p_ask.add_argument("--trace", type=Path, help="trace output path (default: runs/<run_id>.jsonl)")
    p_ask.add_argument("--no-search", action="store_true", help="disable web_search even if TAVILY_API_KEY is set")
    p_ask.add_argument("--view", action="store_true", help="also write an HTML view of the trace")
    p_ask.add_argument("--quiet", action="store_true", help="don't print live events")

    p_view = sub.add_parser("view", help="render a trace as HTML")
    p_view.add_argument("trace", type=Path)
    p_view.add_argument("-o", "--out", type=Path)

    p_replay = sub.add_parser("replay", help="re-run a trace offline and check the loop behaves identically")
    p_replay.add_argument("trace", type=Path)

    p_eval = sub.add_parser("eval", help="run the eval suite")
    _add_run_options(p_eval)
    p_eval.add_argument("--filter", help="only cases whose id contains this string, or that have this tag")
    p_eval.add_argument("--workers", type=int, help="parallel questions (default: 1 for ollama, 4 for anthropic)")
    p_eval.add_argument("--check", action="store_true", help="validate reference SQL only; no model calls")
    p_eval.add_argument("--compare", nargs=2, type=Path, metavar=("A.json", "B.json"))

    p_serve = sub.add_parser("serve", help="open the web app on localhost")
    p_serve.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database (default: %(default)s)")
    p_serve.add_argument("--provider", default=DEFAULT_PROVIDER, choices=PROVIDERS, help="default provider in the page")
    p_serve.add_argument("--model", help="default model in the page")
    p_serve.add_argument("--host", default="127.0.0.1", help="interface to bind (default: %(default)s; keep it local)")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-browser", action="store_true", help="don't open a browser tab")

    p_setup = sub.add_parser("setup-local", help="install a free local model in Ollama")
    p_setup.add_argument("--base", default=LOCAL_BASE_MODEL, help="Ollama model to build on (default: %(default)s)")
    p_setup.add_argument("--ctx", type=int, default=LOCAL_CONTEXT, help="context window (default: %(default)s)")
    p_setup.add_argument("--name", default=DEFAULT_MODELS["ollama"], help="name to create (default: %(default)s)")

    p_load = sub.add_parser("load-data", help="download the dataset and build the SQLite database")
    p_load.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p_load.add_argument("--db", type=Path, default=DEFAULT_DB)
    p_load.add_argument("--skip-download", action="store_true", help="use an existing CSV")

    args = parser.parse_args(argv)

    if args.command == "ask":
        from agent.app import run_question

        if (llm := _make_llm(args)) is None:
            return 2
        result = run_question(
            args.question, db_path=args.db, llm=llm, budget=_budget(args),
            chaos=args.chaos, seed=args.seed, trace_path=args.trace,
            include_search=False if args.no_search else None, listeners=[] if args.quiet else [_live],
            prefetch_schema=args.prefetch_schema,
        )
        print(result.answer or f"(no answer: {result.status})")
        print(f"\n— {result.status} · {result.steps} steps · {result.usage.total_tokens:,} tokens · "
              f"${result.cost_usd:.4f} · {result.duration_ms / 1000:.1f}s · trace {result.trace_path}"
              + (f" · {result.detail}" if result.detail else ""), file=sys.stderr)
        if args.view and result.trace_path:
            from agent.viewer import write_html
            print(f"view: {write_html(result.trace_path)}", file=sys.stderr)
        return 0 if result.status == "completed" else 1

    if args.command == "view":
        from agent.viewer import write_html
        print(write_html(args.trace, args.out))
        return 0

    if args.command == "replay":
        from agent.replay import replay
        report = replay(args.trace)
        print(("OK  " if report.ok else "DIVERGED  ") + report.message)
        return 0 if report.ok else 1

    if args.command == "eval":
        from evals import runner
        if args.compare:
            print(runner.compare(*args.compare))
            return 0
        if args.check:
            return 0 if runner.check(args.db, args.filter) else 1
        if (llm := _make_llm(args)) is None:
            return 2
        workers = args.workers or (1 if llm.provider == "ollama" else 4)  # one local GPU: run serially
        out = runner.run_eval(args.db, llm, filter_=args.filter, chaos=args.chaos, seed=args.seed,
                              workers=workers, budget=_budget(args), prefetch_schema=args.prefetch_schema)
        print((out / "report.md").read_text())
        return 0

    if args.command == "serve":
        return serve(args)

    if args.command == "setup-local":
        return setup_local(args.base, args.ctx, args.name)

    if args.command == "load-data":
        from agent import data
        if not args.skip_download:
            data.download(args.csv)
        print(f"loaded {data.load(args.csv, args.db):,} rows into {args.db}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
