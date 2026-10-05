"""End-to-end tests: the web app in a real browser engine (PRD 007, WEB-1..WEB-17)."""

from __future__ import annotations

import re
import threading

import pytest
from playwright.sync_api import Page, expect

from agent.chaos import Chaos
from agent.errors import TransientError
from tests.conftest import ScriptedLLM, message, text, tool_use
from tests.e2e.conftest import CLAUDE_OFF, LiveApp

pytestmark = pytest.mark.e2e

COUNT_SQL = "SELECT ev_type, COUNT(*) AS n FROM vehicles GROUP BY ev_type ORDER BY ev_type"


def good_run(answer: str = "There are two vehicle types.") -> list:
    return [
        message(tool_use("run_sql", {"sql": COUNT_SQL}), stop_reason="tool_use"),
        message(text(answer)),
    ]


def ask(page: Page, live: LiveApp, question: str = "How many vehicles of each type?") -> None:
    page.goto(live.url)
    page.fill("#question", question)
    page.click("#go")


def wait_for_answer(page: Page):
    final = page.locator(".card.final")
    expect(final).to_be_visible()
    return final


# --- loading the page -----------------------------------------------------------------------


def test_page_shows_status_and_examples(page: Page, live: LiveApp):
    page.goto(live.url)
    expect(page).to_have_title("Ask the Data")
    status = page.locator("#status")
    expect(status).to_contain_text("askdata-local")
    expect(status).to_contain_text("500 vehicles")  # the 500-row test database
    expect(status).to_contain_text("Web search off")

    examples = page.locator("#examples button")
    expect(examples).to_have_count(5)
    examples.first.click()
    expect(page.locator("#question")).to_have_value(examples.first.inner_text())


def test_unavailable_provider_shows_how_to_fix_it(page: Page, live: LiveApp):
    live.statuses = [{**live.statuses[0], "available": False,
                      "message": "Ollama isn't reachable. Start it with `ollama serve`."}, CLAUDE_OFF]
    page.goto(live.url)
    expect(page.locator("#status .pill.bad")).to_contain_text("askdata-local")
    expect(page.locator("#form_error")).to_contain_text("ollama serve")


# --- a run, step by step --------------------------------------------------------------------


def test_run_renders_every_step_with_explanations(page: Page, live: LiveApp):
    live.queue(good_run())
    ask(page, live)
    final = wait_for_answer(page)
    expect(final).to_contain_text("There are two vehicle types.")

    timeline = page.locator("#timeline")
    expect(timeline.locator(".step-head h3")).to_have_text(["Setup", "Step 1", "Step 2"])
    expect(timeline).to_contain_text("stop_reason: tool_use")
    expect(timeline).to_contain_text("stop_reason: end_turn")
    expect(timeline.locator(".card.tool .title").filter(has_text="run_sql result")).to_be_visible()
    # SQL results are rendered as a real table
    expect(timeline.locator("table th")).to_have_text(["ev_type", "n"])
    # every model, tool and request card comes with an explanation
    expect(timeline.locator(".explain").first).to_be_visible()
    expect(timeline).to_contain_text("The model has no memory between calls")

    expect(page.locator('#loop li[data-stage="done"]')).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#stats")).to_contain_text("Steps2")
    expect(page.locator("#stats")).to_contain_text("Tool calls1")
    expect(page.locator("#go")).to_be_enabled()


def test_waiting_indicator_and_loop_stage_while_the_model_thinks(page: Page, live: LiveApp):
    release = threading.Event()

    class SlowLLM(ScriptedLLM):
        def create(self, **kw):
            release.wait(10)
            return super().create(**kw)

    live.queue(SlowLLM([message(text("done"))]))
    ask(page, live)
    expect(page.locator(".waiting")).to_contain_text("Waiting for the model")
    expect(page.locator('#loop li[data-stage="call"]')).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#go")).to_be_disabled()
    release.set()
    wait_for_answer(page)
    expect(page.locator(".waiting")).to_have_count(0)


def test_fixable_error_is_shown_and_explained(page: Page, live: LiveApp):
    live.queue([
        message(tool_use("run_sql", {"sql": "SELECT manufacturer FROM vehicles"}), stop_reason="tool_use"),
        message(tool_use("run_sql", {"sql": COUNT_SQL}), stop_reason="tool_use"),
        message(text("Fixed it.")),
    ])
    ask(page, live)
    wait_for_answer(page)
    error = page.locator(".card.error")
    expect(error).to_contain_text("run_sql failed (execution_error)")
    expect(error).to_contain_text("no such column: manufacturer")
    expect(error.locator(".explain")).to_contain_text("chance to correct itself")
    expect(page.locator("#stats")).to_contain_text("Tool errors1")

    # regression: explanations on error cards once inherited the form's red error style
    explain_colors = page.evaluate("""() => [...document.querySelectorAll('.explain')]
        .map(e => getComputedStyle(e).color)""")
    assert len(set(explain_colors)) == 1, explain_colors


def _seed_that_fails_once() -> int:
    """A chaos seed whose first tool attempt fails and second succeeds."""
    for seed in range(1000):
        chaos = Chaos(0.5, seed)
        outcomes = []
        for _ in range(2):
            try:
                chaos.maybe_fail("run_sql")
                outcomes.append("ok")
            except TransientError:
                outcomes.append("fail")
        if outcomes == ["fail", "ok"]:
            return seed
    raise AssertionError("no suitable seed")


def test_chaos_retries_are_shown(page: Page, live: LiveApp):
    live.queue(good_run())
    page.goto(live.url)
    page.click(".options summary")
    page.locator("#chaos").evaluate("el => { el.value = '0.5'; el.dispatchEvent(new Event('input')); }")
    expect(page.locator("#chaos_out")).to_have_text("50%")
    page.fill("#seed", str(_seed_that_fails_once()))
    page.fill("#question", "How many vehicles of each type?")
    page.click("#go")
    wait_for_answer(page)

    retry = page.locator(".card.warn").filter(has_text="attempt 1 failed")
    expect(retry).to_contain_text("(injected)")
    expect(retry.locator(".explain")).to_contain_text("Chaos mode injected this failure")
    expect(page.locator(".card.tool .meta").filter(has_text="2 attempts")).to_be_visible()
    expect(page.locator("#stats")).to_contain_text("Retries1")


def test_budget_stop_is_shown(page: Page, live: LiveApp):
    live.queue([message(tool_use("get_schema", {}), stop_reason="tool_use")])
    page.goto(live.url)
    page.click(".options summary")
    page.fill("#max_steps", "1")
    page.fill("#question", "Anything at all?")
    page.click("#go")
    final = wait_for_answer(page)
    expect(final).to_contain_text("Run ended: budget_exceeded")
    expect(page.locator(".card.warn").filter(has_text="budget exceeded")).to_be_visible()
    expect(page.locator('#loop li[data-stage="budget"]')).to_have_class(re.compile(r"\bstopped\b"))


# --- views ----------------------------------------------------------------------------------


def test_what_the_model_sees_and_raw_events(page: Page, live: LiveApp):
    live.queue(good_run())
    ask(page, live)
    wait_for_answer(page)

    page.get_by_role("tab", name="What the model sees").click()
    roles = page.locator("#conversation .msg .role")
    expect(roles).to_have_text(["system prompt", "user", "assistant", "user", "assistant"])
    expect(page.locator("#conversation")).to_contain_text("tool_result")
    expect(page.locator("#timeline")).to_be_hidden()

    page.get_by_role("tab", name="Raw events").click()
    events = page.locator("#raw .raw-event")
    expect(events.first).to_contain_text("#1 · step 0 · run_start")
    expect(events.last).to_contain_text("run_end")


def test_explain_toggle_hides_explanations_and_is_remembered(page: Page, live: LiveApp):
    live.queue(good_run())
    ask(page, live)
    wait_for_answer(page)
    explanations = page.locator("#timeline .explain")
    expect(explanations.first).to_be_visible()

    page.uncheck("#explain")
    expect(explanations.first).to_be_hidden()
    page.reload()
    expect(page.locator("#explain")).not_to_be_checked()
    expect(page.locator(".loop-note")).to_be_hidden()


def test_past_runs_and_links_reopen_a_run(page: Page, live: LiveApp):
    live.queue(good_run("Answer from the first run."))
    ask(page, live, "First question?")
    wait_for_answer(page)
    run_link = page.url
    assert re.search(r"#run=[\w-]+$", run_link)

    past = page.locator("#runs button")
    expect(past.first).to_contain_text("First question?")
    expect(past.first).to_contain_text("completed")

    page.goto(live.url)  # fresh page, no run open
    expect(page.locator("#empty")).to_be_visible()
    past.first.click()
    expect(wait_for_answer(page)).to_contain_text("Answer from the first run.")

    page.goto("about:blank")
    page.goto(run_link)  # the run's own link
    expect(wait_for_answer(page)).to_contain_text("Answer from the first run.")


# --- errors, safety, layout -----------------------------------------------------------------


def test_second_run_while_busy_shows_a_message(page: Page, live: LiveApp):
    release = threading.Event()

    class BlockingLLM(ScriptedLLM):
        def create(self, **kw):
            release.wait(10)
            return super().create(**kw)

    live.queue(BlockingLLM([message(text("first"))]))
    page.goto(live.url)
    started = page.request.post(f"{live.url}/api/runs", data={"question": "Started elsewhere"})
    assert started.ok
    page.fill("#question", "Second question?")
    page.click("#go")
    expect(page.locator("#form_error")).to_contain_text("already in progress")
    expect(page.locator("#go")).to_be_enabled()
    release.set()


def test_model_output_is_shown_as_text_not_html(page: Page, live: LiveApp):
    payload = '<img src=x onerror="window.__pwned = 1"><b>bold?</b>'
    live.queue([
        message(text(payload), tool_use("run_sql", {"sql": "SELECT 1 AS x"}), stop_reason="tool_use"),
        message(text(payload)),
    ])
    ask(page, live)
    final = wait_for_answer(page)
    expect(final).to_contain_text(payload)
    # the payload's own tags must not exist as elements (the page's <b> tool labels are fine)
    assert page.locator("#timeline img").count() == 0
    assert page.locator("#timeline b", has_text="bold?").count() == 0
    assert page.evaluate("() => window.__pwned") is None


def test_phone_width_has_no_horizontal_scrolling(page: Page, live: LiveApp):
    page.set_viewport_size({"width": 390, "height": 844})
    live.queue(good_run())
    ask(page, live)
    wait_for_answer(page)
    overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"page is {overflow}px wider than the viewport"


def test_dark_mode_follows_the_system_setting(browser, live: LiveApp):
    context = browser.new_context(color_scheme="dark")
    page = context.new_page()
    page.goto(live.url)
    background = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
    assert background == "rgb(15, 14, 13)"
    context.close()
