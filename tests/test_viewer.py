import re

from agent.trace import Tracer
from agent.viewer import render


def test_viewer_is_self_contained_and_escaped():
    t = Tracer("r1")
    t.emit("run_start", 0, question="<script>alert(1)</script>", model="m", config={}, tools=[])
    t.emit("model_response", 1, stop_reason="tool_use", usage={"input_tokens": 5}, cost_usd=0.001, duration_ms=10,
           response={"content": [{"type": "tool_use", "id": "a", "name": "run_sql", "input": {"sql": "SELECT 1"}}]})
    t.emit("retry", 1, target="run_sql", attempt=1, delay_s=0.5, error="503", injected=True)
    t.emit("tool_result", 1, tool_use_id="a", name="run_sql", content="no such column", is_error=True,
           error_kind="execution_error", attempts=2, duration_ms=3)
    t.emit("run_end", 1, status="completed", answer="42", steps=1, usage={"input_tokens": 5}, cost_usd=0.001,
           duration_ms=10, detail=None)
    html = render(t.events)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "injected by chaos" in html and "execution_error" in html
    assert not re.search(r"<(script|link)[^>]+(src|href)=", html)  # no external resources
