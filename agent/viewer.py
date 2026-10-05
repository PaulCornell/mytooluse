"""Render a JSONL trace as a self-contained HTML timeline (PRD 004, OBS-4)."""

from __future__ import annotations

import json
from html import escape
from itertools import groupby
from pathlib import Path
from typing import Any

from agent.trace import read_trace

_CSS = """
:root { --bg:#fafaf9; --card:#fff; --fg:#1c1917; --muted:#78716c; --border:#e7e5e4;
  --accent:#2563eb; --ok:#15803d; --err:#b91c1c; --err-bg:#fef2f2; --warn:#b45309; --warn-bg:#fffbeb;
  --code-bg:#f5f5f4; }
@media (prefers-color-scheme: dark) { :root { --bg:#0c0a09; --card:#1c1917; --fg:#f5f5f4; --muted:#a8a29e;
  --border:#292524; --accent:#60a5fa; --ok:#4ade80; --err:#f87171; --err-bg:#2a1414; --warn:#fbbf24;
  --warn-bg:#2a2010; --code-bg:#0c0a09; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 ui-sans-serif,system-ui,sans-serif; }
main { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:20px; margin:0 0 4px; } .muted { color:var(--muted); font-size:13px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px; margin:16px 0; }
.stat { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:8px 12px; }
.stat b { display:block; font-size:18px; font-variant-numeric:tabular-nums; }
.answer { background:var(--card); border:1px solid var(--border); border-left:4px solid var(--ok);
  border-radius:8px; padding:12px 16px; white-space:pre-wrap; }
.answer.bad { border-left-color:var(--err); }
.step { margin-top:28px; } .step h2 { font-size:14px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); margin:0 0 8px; border-bottom:1px solid var(--border); padding-bottom:4px; }
.ev { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:10px 14px; margin:8px 0; }
.ev.error { background:var(--err-bg); border-color:var(--err); } .ev.retry, .ev.guard { background:var(--warn-bg); border-color:var(--warn); }
.tag { display:inline-block; font-size:12px; font-weight:600; padding:1px 8px; border-radius:99px;
  border:1px solid currentColor; margin-right:6px; }
.tag.model { color:var(--accent); } .tag.tool { color:var(--ok); } .tag.err { color:var(--err); } .tag.warn { color:var(--warn); }
pre { background:var(--code-bg); border:1px solid var(--border); border-radius:6px; padding:8px 10px;
  overflow-x:auto; font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre-wrap; word-break:break-word; margin:6px 0 0; }
details summary { cursor:pointer; color:var(--muted); font-size:13px; margin-top:6px; }
.meta { float:right; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.text { white-space:pre-wrap; margin-top:6px; }
"""


def render(events: list[dict[str, Any]]) -> str:
    start = next((e for e in events if e["type"] == "run_start"), {})
    end = next((e for e in events if e["type"] == "run_end"), None)
    retries = sum(1 for e in events if e["type"] == "retry")
    tool_errors = sum(1 for e in events if e["type"] == "tool_result" and e.get("is_error"))
    tool_calls = sum(1 for e in events if e["type"] == "tool_call")

    status = end["status"] if end else "incomplete"
    usage = (end or {}).get("usage", {})
    tokens = sum(usage.values()) if usage else 0
    stats = [
        ("Status", status), ("Steps", (end or {}).get("steps", "?")), ("Tool calls", tool_calls),
        ("Tool errors", tool_errors), ("Retries", retries), ("Tokens", f"{tokens:,}"),
        ("Cost", f"${(end or {}).get('cost_usd', 0):.4f}"), ("Time", f"{(end or {}).get('duration_ms', 0) / 1000:.1f}s"),
    ]
    parts = [
        f"<h1>{escape(start.get('question', '(no question)'))}</h1>",
        f"<div class=muted>run {escape(str(start.get('run_id', '')))} · {escape(str(start.get('provider', 'anthropic')))}"
        f" / {escape(str(start.get('model', '')))}"
        f" · {escape(str(start.get('ts', '')))}</div>",
        "<div class=stats>" + "".join(f"<div class=stat><span class=muted>{k}</span><b>{escape(str(v))}</b></div>"
                                     for k, v in stats) + "</div>",
    ]
    if end:
        bad = "" if status == "completed" else " bad"
        detail = f"\n\n[{end['detail']}]" if end.get("detail") else ""
        parts.append(f"<div class='answer{bad}'>{escape((end.get('answer') or '(no answer)') + detail)}</div>")

    body = [e for e in events if e["type"] not in ("run_start", "run_end")]
    for step, group in groupby(body, key=lambda e: e["step"]):
        heading = "Before step 1 · fetched by the harness" if step == 0 else f"Step {step}"
        parts.append(f"<section class=step><h2>{heading}</h2>")
        parts.extend(_event(e) for e in group if e["type"] != "model_request")
        parts.append("</section>")

    title = escape(start.get("question", "Agent trace"))[:80]
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><title>Trace: {title}</title>"
            f"<style>{_CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")


def _event(e: dict[str, Any]) -> str:
    t = e["type"]
    if t == "model_response":
        u = e.get("usage", {})
        meta = (f"{e.get('duration_ms', 0) / 1000:.1f}s · in {u.get('input_tokens', 0):,} · "
                f"cache {u.get('cache_read_tokens', 0):,} · out {u.get('output_tokens', 0):,} · ${e.get('cost_usd', 0):.4f}")
        inner = []
        for block in e["response"].get("content", []):
            bt = block.get("type")
            if bt == "thinking" and block.get("thinking"):
                inner.append(f"<details><summary>reasoning summary</summary><pre>{escape(block['thinking'])}</pre></details>")
            elif bt == "text" and block.get("text", "").strip():
                inner.append(f"<div class=text>{escape(block['text'])}</div>")
            elif bt == "tool_use":
                inner.append(f"<pre>→ {escape(block['name'])}({escape(_fmt_input(block['input']))})</pre>")
            elif bt == "fallback":
                inner.append(f"<div class=muted>fallback: {escape(json.dumps(block))}</div>")
        return (f"<div class=ev><span class='tag model'>model</span><b>stop: {escape(str(e.get('stop_reason')))}</b>"
                f"<span class=meta>{meta}</span>{''.join(inner)}</div>")
    if t == "tool_result":
        err = e.get("is_error")
        tag = f"<span class='tag {'err' if err else 'tool'}'>{escape(e['name'])}</span>"
        label = f"error: {escape(str(e.get('error_kind')))}" if err else "result"
        meta = f"{e.get('duration_ms', 0)} ms · attempts {e.get('attempts', 1)}"
        content = e.get("content", "")
        body = (f"<pre>{escape(content)}</pre>" if len(content) < 1500 or err
                else f"<details><summary>{len(content):,} chars</summary><pre>{escape(content)}</pre></details>")
        return f"<div class='ev{' error' if err else ''}'>{tag}<b>{label}</b><span class=meta>{meta}</span>{body}</div>"
    if t == "retry":
        injected = " (injected by chaos)" if e.get("injected") else ""
        return (f"<div class='ev retry'><span class='tag warn'>retry</span><b>{escape(e['target'])} attempt "
                f"{e['attempt']} failed{injected}</b><span class=meta>backoff {e['delay_s']:.2f}s</span>"
                f"<div class=text>{escape(e.get('error', ''))}</div></div>")
    if t == "guard":
        return (f"<div class='ev guard'><span class='tag warn'>guard</span><b>{escape(e['reason'])}</b>"
                f"<div class=text>{escape(str(e.get('detail', '')))}</div></div>")
    return ""  # tool_call is shown inside the model card


def _fmt_input(value: Any) -> str:
    if isinstance(value, dict) and len(value) == 1:
        (v,) = value.values()
        if isinstance(v, str) and ("\n" in v or len(v) > 60):
            return "\n" + v
    return json.dumps(value, ensure_ascii=False)


def write_html(trace_path: Path, out_path: Path | None = None) -> Path:
    out_path = out_path or trace_path.with_suffix(".html")
    out_path.write_text(render(read_trace(trace_path)), encoding="utf-8")
    return out_path
