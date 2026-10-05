// Ask the Data: renders the agent's trace events live, with an explanation of each one.
// Security: every value from the server is inserted as text (textContent), never as HTML,
// because tool results can contain untrusted text such as web search snippets.
"use strict";

// ---------------------------------------------------------------------------------------
// Explanations shown in "Explain each step" mode
// ---------------------------------------------------------------------------------------

const EXPLAIN = {
  run_start:
    "The loop starts. Your question becomes the first message in the conversation. Every request to the model " +
    "also carries a system prompt (standing instructions) and the list of tools, each with a JSON schema for its " +
    "inputs. The model can't run anything itself: it can only ask the harness to run a tool.",
  prefetch:
    "Before the first model call, the harness ran get_schema itself and put the result in the first message. " +
    "Small local models do better when they don't have to discover the tables and columns first.",
  messages_first:
    "The first request. The model has no memory between calls, so each request resends the whole conversation. " +
    "Right now that's just your question.",
  messages_more:
    "The model has no memory between calls, so each request resends the whole conversation. These are the " +
    "messages added since the last request: the model's previous reply, and a user message carrying the tool results.",
  waiting:
    "Waiting for the model to respond. A local model on a laptop can take from a few seconds to a minute.",
  stop: {
    tool_use: (n) =>
      `stop_reason = "tool_use": the model wants ${n} tool call${n === 1 ? "" : "s"}. The harness validates each ` +
      "input against the tool's schema, runs it, and sends every result back in one message. Then the loop repeats.",
    end_turn: () => 'stop_reason = "end_turn": the model is done, and its text is the final answer. The loop ends.',
    max_tokens: () => 'stop_reason = "max_tokens": the reply hit the output limit, so the answer is cut off.',
    refusal: () => 'stop_reason = "refusal": the model declined to answer. The harness stops and reports it.',
    pause_turn: () => 'stop_reason = "pause_turn": the server paused a long turn. The harness re-sends to continue.',
  },
  usage:
    "Tokens: \"in\" is new prompt text the model read, \"cached\" is prompt text reused from a cache (faster, and " +
    "cheaper on paid APIs), and \"out\" is what the model wrote. Local models cost nothing.",
  thinking: "The model's reasoning before it acted (a summary, where the model provides one).",
  tool: {
    get_schema: "Lists the tables, columns and common values, so the model can write SQL with real names.",
    run_sql:
      "Runs one read-only SQL query on the SQLite database. Writes are blocked three ways: a read-only " +
      "connection, query_only mode, and an authorizer that allows only reads. Slow queries are cancelled.",
    run_python:
      "Runs a Python script in a separate process with no API keys in its environment, CPU and file limits, " +
      "and a timeout. On macOS it also has no network access and can only write to its own folder.",
    web_search: "Searches the web. Results are labeled as untrusted data, so the model treats them as evidence, not instructions.",
  },
  result_ok:
    "The result goes back to the model as a tool_result block, matched to the call by its tool_use_id.",
  error: {
    execution_error:
      "The tool failed in a way the model can fix, such as a wrong column name. The error goes back with " +
      "is_error: true, and the model gets a chance to correct itself on the next step.",
    invalid_input:
      "The model's input didn't match the tool's schema, so the tool never ran. The validation message goes back to the model.",
    guard_blocked:
      "The model repeated an identical call. The harness blocked it without running it and told the model to change approach.",
    transient_exhausted:
      "The tool kept failing with temporary errors even after retries. The model is told, so it can work around it.",
    internal: "A bug inside the tool. The run continues, and the model is told the tool failed.",
  },
  retry: (e) =>
    (e.injected ? "Chaos mode injected this failure. " : "") +
    "A temporary failure, such as a rate limit or timeout. The harness waits (exponential backoff with random " +
    "jitter) and retries automatically. The model never sees it unless every retry fails.",
  guard: {
    final_step:
      "This is the last step the budget allows. The harness tells the model to answer now and stops offering tools.",
    budget_exceeded: "A budget ran out (steps, tokens, cost or time), so the harness stopped the run.",
    loop_block: "The loop guard blocked a repeated identical tool call.",
    loop_abort: "The model kept repeating the same call, so the loop guard ended the run.",
    context_near_limit:
      "The prompt is close to the model's context window. Ollama silently drops the start of the conversation " +
      "when it overflows, so answers may get worse from here.",
  },
  status: {
    completed: "The model finished with an answer.",
    budget_exceeded: "The run stopped because a budget ran out. The answer shown is the last text the model wrote, if any.",
    aborted: "The loop guard stopped the run.",
    refused: "The model declined.",
    truncated: "The final reply hit the output limit.",
    error: "The run stopped on an error that retrying can't fix, such as a missing API key.",
  },
  answer_caveat:
    "This is the model's answer, not a verified fact. Compare it with the tool results above: small models " +
    "sometimes misquote their own queries.",
};

// ---------------------------------------------------------------------------------------
// DOM helpers (text only, never innerHTML)
// ---------------------------------------------------------------------------------------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);
const explain = (text) => el("p", { class: "explain", text });
const pre = (text) => el("pre", { text });
const fmt = (n) => (n ?? 0).toLocaleString();
const secs = (ms) => `${((ms ?? 0) / 1000).toFixed(1)}s`;

// Markdown tables from run_sql become real tables; anything else stays preformatted.
function renderContent(text) {
  const lines = (text || "").split("\n");
  const tableLines = lines.filter((l) => l.startsWith("|"));
  if (tableLines.length >= 2 && lines[0].startsWith("|") && /^\|(-+\|)+$/.test(lines[1].replace(/\s/g, ""))) {
    const cells = (line) => line.slice(1, -1).split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
    const [head, , ...body] = tableLines;
    const table = el("table", {},
      el("thead", {}, el("tr", {}, cells(head).map((c) => el("th", { text: c })))),
      el("tbody", {}, body.map((row) => el("tr", {}, cells(row).map((c) => el("td", { text: c }))))));
    const rest = lines.filter((l) => !l.startsWith("|")).join("\n").trim();
    return el("div", {}, el("div", { class: "table-wrap" }, table), rest && el("p", { class: "hint", text: rest }));
  }
  return pre(text);
}

function collapsible(summary, body, open = false) {
  return el("details", { open }, el("summary", { text: summary }), body);
}

function inputText(input) {
  if (!input || typeof input !== "object") return JSON.stringify(input);
  const values = Object.values(input);
  if (values.length === 1 && typeof values[0] === "string") return values[0];
  return JSON.stringify(input, null, 2);
}

// ---------------------------------------------------------------------------------------
// Run view state
// ---------------------------------------------------------------------------------------

let view = null;
let source = null;
let config = null;

function resetView(runId) {
  if (source) source.close();
  view = {
    runId, steps: new Map(), pending: new Map(), conversation: [], lastResponse: null, tools: [],
    stats: { steps: 0, toolCalls: 0, toolErrors: 0, retries: 0, tokens: 0, cost: 0 },
    started: null, ended: false, timer: null,
  };
  for (const id of ["timeline", "conversation", "raw"]) $(id).replaceChildren();
  $("empty").hidden = true;
  setStage(null);
  renderStats();
}

function stepSection(step) {
  if (!view.steps.has(step)) {
    const title = step === 0 ? "Setup" : `Step ${step}`;
    const note = step === 0 ? "before the loop starts" : "one trip around the loop";
    const body = el("div");
    const section = el("section", { class: "step" },
      el("div", { class: "step-head" }, el("h3", { text: title }), el("span", { text: note })), body);
    $("timeline").append(section);
    view.steps.set(step, body);
  }
  return view.steps.get(step);
}

function addCard(step, kind, head, ...body) {
  const card = el("div", { class: `card ${kind}` }, head, ...body);
  stepSection(step).append(card);
  if (!view.replaying) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  return card;
}

function cardHead(tag, tagClass, title, meta) {
  return el("div", { class: "card-head" },
    el("span", { class: `tag ${tagClass}`, text: tag }),
    el("span", { class: "title", text: title }),
    meta && el("span", { class: "meta", text: meta }));
}

function waiting(text) {
  const label = el("span", { text });
  const row = el("div", { class: "waiting" }, el("span", { class: "spinner", "aria-hidden": "true" }), label);
  const started = Date.now();
  const timer = setInterval(() => { label.textContent = `${text} ${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
  row.stop = () => { clearInterval(timer); row.remove(); };
  return row;
}

function setStage(stage, stopped = false) {
  for (const li of $("loop").children) {
    li.classList.toggle("active", li.dataset.stage === stage && !stopped);
    li.classList.toggle("stopped", stopped && li.dataset.stage === stage);
  }
}

function renderStats() {
  const s = view ? view.stats : { steps: 0, toolCalls: 0, toolErrors: 0, retries: 0, tokens: 0, cost: 0 };
  const elapsed = view && view.started ? ((view.endedAt || Date.now()) - view.started) / 1000 : 0;
  const items = [
    ["Steps", s.steps], ["Tool calls", s.toolCalls], ["Tool errors", s.toolErrors], ["Retries", s.retries],
    ["Tokens", fmt(s.tokens)], ["Cost", `$${s.cost.toFixed(4)}`], ["Time", `${elapsed.toFixed(0)}s`],
  ];
  $("stats").replaceChildren(...items.map(([k, v]) => el("div", { class: "stat" }, el("span", { text: k }), el("b", { text: v }))));
}

function stopPending(key) {
  const row = view.pending.get(key);
  if (row) { row.stop(); view.pending.delete(key); }
}

// ---------------------------------------------------------------------------------------
// Event handlers: one per trace event type
// ---------------------------------------------------------------------------------------

const handlers = {
  run_start(e) {
    view.started = Date.parse(e.ts);
    view.tools = e.tools || [];
    if (!view.replaying) view.timer = setInterval(renderStats, 1000);
    const toolList = el("div", {}, view.tools.map((t) =>
      el("div", { class: "block" }, el("b", { text: t.name }), " — ", t.description,
        collapsible("input schema", pre(JSON.stringify(t.input_schema, null, 2))))));
    addCard(0, "setup", cardHead("start", "muted", e.question, `${e.provider || "anthropic"} / ${e.model}`),
      explain(EXPLAIN.run_start),
      e.system && collapsible("System prompt", pre(e.system)),
      collapsible(`Tools offered to the model (${view.tools.length})`, toolList));
    view.conversation.push({ role: "system", content: e.system || "(system prompt not recorded in this trace)" });
    setStage("budget");
  },

  messages_added(e) {
    for (const m of e.messages) view.conversation.push(m);
    renderConversation();
    const first = e.total === e.messages.length;
    addCard(e.step, "setup", cardHead("request", "muted", `Sending ${e.total} message${e.total === 1 ? "" : "s"} to the model`),
      explain(first ? EXPLAIN.messages_first : EXPLAIN.messages_more),
      collapsible(`New in this request (${e.messages.length})`, el("div", {}, e.messages.map(renderMessage))));
    setStage("call");
  },

  model_request(e) {
    view.stats.steps = Math.max(view.stats.steps, e.step);
    stopPending("model");
    const row = waiting(e.attempt > 1 ? `Waiting for the model (attempt ${e.attempt})…` : "Waiting for the model…");
    view.pending.set("model", row);
    const card = addCard(e.step, "model", cardHead("model", "model", "Model is thinking"), row,
      e.attempt === 1 && explain(EXPLAIN.waiting));
    row.card = card;
    setStage("call");
  },

  model_response(e) {
    const row = view.pending.get("model");
    if (row && row.card) row.card.remove();
    stopPending("model");
    const u = e.usage || {};
    view.stats.tokens += (u.input_tokens || 0) + (u.output_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_write_tokens || 0);
    view.stats.cost += e.cost_usd || 0;
    view.lastResponse = e.response;
    const blocks = e.response.content || [];
    const toolUses = blocks.filter((b) => b.type === "tool_use");
    const body = [];
    for (const b of blocks) {
      if (b.type === "thinking" && b.thinking) body.push(collapsible("Reasoning", el("div", {}, explain(EXPLAIN.thinking), pre(b.thinking))));
      if (b.type === "text" && b.text.trim()) {
        body.push(e.stop_reason === "end_turn"
          ? el("p", { class: "hint", text: "The model's text becomes the final answer, shown below." })
          : el("div", { class: "text", text: b.text }));
      }
      if (b.type === "tool_use") body.push(el("div", { class: "block" },
        el("span", { class: "kind", text: `tool_use → ${b.name}` }), pre(inputText(b.input))));
    }
    const why = EXPLAIN.stop[e.stop_reason];
    const meta = `${secs(e.duration_ms)} · in ${fmt(u.input_tokens)} · cached ${fmt(u.cache_read_tokens)} · out ${fmt(u.output_tokens)}`;
    const firstResponse = !view.sawResponse;
    view.sawResponse = true;
    addCard(e.step, "model", cardHead("model", "model", `stop_reason: ${e.stop_reason}`, meta),
      ...body, why && explain(why(toolUses.length)), firstResponse && explain(EXPLAIN.usage));
    renderStats();
    setStage("decide");
  },

  tool_call(e) {
    if (e.tool_use_id === "prefetch") {
      view.pending.set(e.tool_use_id, waiting("Harness is reading the schema…"));
      addCard(0, "tool", cardHead("harness", "tool", "get_schema (prefetch)"), view.pending.get(e.tool_use_id),
        explain(EXPLAIN.prefetch));
      return;
    }
    view.stats.toolCalls += 1;
    const row = waiting(`Running ${e.name}…`);
    view.pending.set(e.tool_use_id, row);
    addCard(e.step, "tool", cardHead("tool call", "tool", e.name), pre(inputText(e.input)), row,
      EXPLAIN.tool[e.name] && explain(EXPLAIN.tool[e.name]));
    setStage("tools");
  },

  tool_result(e) {
    stopPending(e.tool_use_id);
    if (e.is_error) view.stats.toolErrors += 1;
    const meta = `${e.duration_ms} ms${e.attempts > 1 ? ` · ${e.attempts} attempts` : ""}`;
    const title = e.is_error ? `${e.name} failed (${e.error_kind})` : `${e.name} result`;
    const content = e.content.length > 2500 && !e.is_error
      ? collapsible(`Show result (${fmt(e.content.length)} characters)`, renderContent(e.content))
      : renderContent(e.content);
    const why = e.is_error ? EXPLAIN.error[e.error_kind] : (e.tool_use_id === "prefetch" ? null : EXPLAIN.result_ok);
    addCard(e.step, e.is_error ? "error" : "tool",
      cardHead(e.is_error ? "error" : "result", e.is_error ? "err" : "tool", title, meta), content, why && explain(why));
    renderStats();
    if (e.step > 0) setStage("append");
  },

  retry(e) {
    view.stats.retries += 1;
    addCard(e.step, "warn", cardHead("retry", "warn", `${e.target}: attempt ${e.attempt} failed`, `waiting ${e.delay_s.toFixed(2)}s`),
      el("div", { class: "text", text: e.error }), explain(EXPLAIN.retry(e)));
    renderStats();
  },

  guard(e) {
    addCard(e.step, "warn", cardHead("guard", "warn", e.reason.replaceAll("_", " ")),
      el("div", { class: "text", text: e.detail || "" }), EXPLAIN.guard[e.reason] && explain(EXPLAIN.guard[e.reason]));
    if (e.reason === "budget_exceeded" || e.reason === "loop_abort") setStage("budget", true);
  },

  run_end(e) {
    for (const key of [...view.pending.keys()]) stopPending(key);
    view.ended = true;
    view.endedAt = Date.parse(e.ts);
    clearInterval(view.timer);
    view.stats.steps = e.steps;
    view.stats.cost = e.cost_usd || view.stats.cost;
    if (view.lastResponse && e.status === "completed") {
      view.conversation.push({ role: "assistant", content: view.lastResponse.content });
      renderConversation();
    }
    const ok = e.status === "completed";
    addCard(e.steps, `final${ok ? "" : " bad"}`,
      cardHead(ok ? "answer" : e.status, ok ? "tool" : "err", ok ? "Final answer" : `Run ended: ${e.status}`,
        `${e.steps} steps · ${secs(e.duration_ms)} · $${(e.cost_usd || 0).toFixed(4)}`),
      el("div", { class: "text", text: e.answer || "(no answer)" }),
      e.detail && el("p", { class: "hint", text: e.detail }),
      explain(EXPLAIN.status[e.status] || ""), ok && explain(EXPLAIN.answer_caveat));
    renderStats();
    setStage(ok ? "done" : "budget", !ok);
    finishRun();
  },

  server_error(e) {
    addCard(0, "error", cardHead("error", "err", "The server hit an error"), pre(e.message));
    finishRun();
  },
};

function renderMessage(m) {
  const blocks = typeof m.content === "string" ? [{ type: "text", text: m.content }] : m.content;
  return el("div", { class: `msg ${m.role}` }, el("div", { class: "role", text: m.role }),
    blocks.map((b) => {
      if (b.type === "text") return el("div", { class: "block" }, el("div", { class: "text", text: b.text }));
      if (b.type === "tool_use") return el("div", { class: "block" },
        el("span", { class: "kind", text: `tool_use · ${b.name} · id ${b.id}` }), pre(inputText(b.input)));
      if (b.type === "tool_result") {
        const text = typeof b.content === "string" ? b.content : JSON.stringify(b.content);
        return el("div", { class: "block" },
          el("span", { class: "kind", text: `tool_result · for ${b.tool_use_id}${b.is_error ? " · is_error: true" : ""}` }),
          pre(text.length > 1500 ? text.slice(0, 1500) + "\n…" : text));
      }
      if (b.type === "thinking") return collapsible("thinking", pre(b.thinking || "(hidden)"));
      return el("div", { class: "block" }, el("span", { class: "kind", text: b.type }), pre(JSON.stringify(b, null, 2)));
    }));
}

function renderConversation() {
  const box = $("conversation");
  box.replaceChildren(
    explain("This is exactly what the model receives on each request: the system prompt, then every message so far. " +
      "The model is stateless, so the harness resends all of it every time. Tool results travel as user messages."),
    ...view.conversation.map((m) => m.role === "system"
      ? el("div", { class: "msg system" }, el("div", { class: "role", text: "system prompt" }), collapsible("show", pre(m.content)))
      : renderMessage(m)));
}

function handle(event) {
  if (!view || event.run_id && !event.run_id.startsWith(view.runId)) return;
  $("raw").append(el("details", { class: "raw-event" },
    el("summary", { text: `#${event.seq} · step ${event.step} · ${event.type}` }), pre(JSON.stringify(event, null, 2))));
  (handlers[event.type] || (() => {}))(event);
}

// ---------------------------------------------------------------------------------------
// Streaming, starting runs, past runs, config
// ---------------------------------------------------------------------------------------

function watch(runId, { replaying = false } = {}) {
  resetView(runId);
  history.replaceState(null, "", `#run=${encodeURIComponent(runId)}`);  // a link to this run
  view.replaying = replaying;
  source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  source.onmessage = (msg) => handle(JSON.parse(msg.data));
  source.addEventListener("end", () => { source.close(); if (!view.ended) finishRun(); });
  source.onerror = () => { if (view && view.ended) source.close(); };
}

function finishRun() {
  if (view) clearInterval(view.timer);
  $("go").disabled = false;
  $("go").textContent = "Ask";
  loadRuns();
}

async function startRun(evt) {
  evt.preventDefault();
  const err = $("form_error");
  err.hidden = true;
  const prefetch = $("prefetch").value;
  const body = {
    question: $("question").value.trim(),
    provider: $("provider").value || undefined,
    model: $("model").value.trim() || undefined,
    max_steps: Number($("max_steps").value),
    max_time_s: Number($("max_time_s").value),
    chaos: Number($("chaos").value),
    seed: $("seed").value === "" ? undefined : Number($("seed").value),
    prefetch_schema: prefetch === "" ? undefined : prefetch === "true",
  };
  $("go").disabled = true;
  $("go").textContent = "Running…";
  try {
    const resp = await fetch("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    watch(data.run_id);
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
    $("go").disabled = false;
    $("go").textContent = "Ask";
  }
}

async function loadRuns() {
  const runs = await fetch("/api/runs").then((r) => r.json()).catch(() => []);
  $("runs").replaceChildren(...(runs.length ? runs.map((r) => el("li", {},
    el("button", { type: "button", onclick: () => watch(r.run_id, { replaying: true }) },
      el("span", { class: "q", text: r.question }),
      el("span", { class: "meta", text: `${r.status} · ${r.model} · ${new Date(r.ts).toLocaleString()}` }))))
    : [el("li", { class: "hint", text: "No runs yet." })]));
}

async function loadConfig() {
  config = await fetch("/api/config").then((r) => r.json());
  const pills = [];
  const provider = $("provider");
  provider.replaceChildren(...config.providers.map((p) =>
    el("option", { value: p.id, disabled: !p.available, selected: p.id === config.default_provider },
      `${p.label}${p.available ? "" : " (unavailable)"}`)));
  const current = config.providers.find((p) => p.id === config.default_provider);
  $("model").placeholder = current ? current.model : "default";
  const problems = [];
  for (const p of config.providers) {
    if (p.id === config.default_provider) {
      pills.push(el("span", { class: `pill ${p.available ? (p.warnings.length ? "warn" : "ok") : "bad"}`,
        text: `${p.label}: ${p.model}` }));
      if (!p.available) problems.push(p.message);
      problems.push(...p.warnings);
    }
  }
  const db = config.database;
  pills.push(el("span", { class: `pill ${db.ok ? "ok" : "bad"}`, text: db.ok ? `${fmt(db.rows)} vehicles` : "No database" }));
  if (!db.ok) problems.push(db.message);
  pills.push(el("span", { class: `pill ${config.search ? "ok" : ""}`, text: config.search ? "Web search on" : "Web search off" }));
  $("status").replaceChildren(...pills);
  if (problems.length) {
    $("form_error").textContent = problems.join("\n\n");
    $("form_error").hidden = false;
  }
  $("examples").replaceChildren(...config.examples.map((q) =>
    el("button", { type: "button", onclick: () => { $("question").value = q; $("question").focus(); } }, q)));
}

function setupTabs() {
  for (const tab of document.querySelectorAll("[role=tab]")) {
    tab.addEventListener("click", () => {
      for (const t of document.querySelectorAll("[role=tab]")) {
        const selected = t === tab;
        t.setAttribute("aria-selected", String(selected));
        $(t.dataset.tab).hidden = !selected;
      }
    });
  }
}

function setupExplainToggle() {
  const box = $("explain");
  try { box.checked = localStorage.getItem("askdata.explain") !== "off"; } catch { /* storage unavailable */ }
  const apply = () => {
    document.body.classList.toggle("no-explain", !box.checked);
    try { localStorage.setItem("askdata.explain", box.checked ? "on" : "off"); } catch { /* ignore */ }
  };
  box.addEventListener("change", apply);
  apply();
}

$("ask").addEventListener("submit", startRun);
$("chaos").addEventListener("input", () => { $("chaos_out").textContent = `${Math.round($("chaos").value * 100)}%`; });
$("provider").addEventListener("change", () => {
  const p = config && config.providers.find((x) => x.id === $("provider").value);
  if (p) $("model").placeholder = p.model;
});
setupTabs();
setupExplainToggle();
loadConfig();
loadRuns();
const linked = /^#run=([A-Za-z0-9_-]{1,64})$/.exec(location.hash);
if (linked) watch(linked[1], { replaying: true });
