import json
import threading

import pytest
from fastapi.testclient import TestClient

from agent.llm import LocalModelError
from agent.web.server import create_app
from tests.conftest import ScriptedLLM, message, text, tool_use

BASE = "http://127.0.0.1"


def scripted_factory(*scripts):
    """Each new run gets the next script."""
    queue = list(scripts)
    return lambda provider, model: ScriptedLLM(queue.pop(0))


def make_client(db_path, tmp_path, factory, **kw):
    app = create_app(db_path=db_path, default_provider="ollama", runs_dir=tmp_path / "runs",
                     llm_factory=factory, include_search=False, **kw)
    return TestClient(app, base_url=BASE)


def read_stream(client, run_id):
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                events.append(json.loads(line[6:]))
            if line == "event: end":
                break
    return events


def happy_script():
    return [
        message(tool_use("run_sql", {"sql": "SELECT COUNT(*) AS n FROM vehicles"}), stop_reason="tool_use"),
        message(text("There are 500 vehicles.")),
    ]


def test_page_and_assets_are_served(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory())
    assert "Ask the Data" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_run_streams_every_event_in_order(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    resp = client.post("/api/runs", json={"question": "How many vehicles?", "prefetch_schema": False})
    assert resp.status_code == 200
    events = read_stream(client, resp.json()["run_id"])

    types = [e["type"] for e in events]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert {"messages_added", "model_request", "model_response", "tool_call", "tool_result"} <= set(types)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["answer"] == "There are 500 vehicles."
    second_request = [e for e in events if e["type"] == "messages_added"][1]
    assert [m["role"] for m in second_request["messages"]] == ["assistant", "user"]
    assert second_request["messages"][1]["content"][0]["type"] == "tool_result"


def test_reconnect_resumes_after_last_event_id(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    run_id = client.post("/api/runs", json={"question": "How many vehicles?"}).json()["run_id"]
    total = len(read_stream(client, run_id))
    with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "3"}) as resp:
        seqs = [json.loads(l[6:])["seq"] for l in resp.iter_lines() if l.startswith("data: ") and l != "data: {}"]
    assert seqs == list(range(4, total + 1))


def test_one_run_at_a_time(db_path, tmp_path):
    release = threading.Event()

    class BlockingLLM(ScriptedLLM):
        def create(self, **kw):
            release.wait(5)
            return super().create(**kw)

    built = []

    def factory(provider, model):
        built.append(model)
        return BlockingLLM([message(text("done"))])

    client = make_client(db_path, tmp_path, factory)
    first = client.post("/api/runs", json={"question": "first question"})
    assert first.status_code == 200
    assert client.get("/api/config").json()["busy"] is True
    second = client.post("/api/runs", json={"question": "second question"})
    assert second.status_code == 409  # refused before the model factory runs (it would fail: no script left)
    assert len(built) == 1  # the busy request never built a model client (no wasted Ollama preflight)
    release.set()
    read_stream(client, first.json()["run_id"])
    assert client.post("/api/runs", json={"question": "third question"}).status_code == 200


def test_past_runs_are_listed_and_replayed_from_disk(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    run_id = client.post("/api/runs", json={"question": "How many vehicles?"}).json()["run_id"]
    live = read_stream(client, run_id)

    fresh = make_client(db_path, tmp_path, scripted_factory())  # new server: run is only on disk
    runs = fresh.get("/api/runs").json()
    assert runs[0]["run_id"] == run_id and runs[0]["status"] == "completed"
    assert read_stream(fresh, run_id) == live


def test_preflight_error_is_returned_to_the_page(db_path, tmp_path):
    def broken(provider, model):
        raise LocalModelError("Ollama isn't reachable. Start it with `ollama serve`.")
    client = make_client(db_path, tmp_path, broken)
    resp = client.post("/api/runs", json={"question": "anything at all"})
    assert resp.status_code == 400 and "ollama serve" in resp.json()["detail"]


def test_crash_in_run_becomes_server_error_event(db_path, tmp_path):
    class Crashing(ScriptedLLM):
        def create(self, **kw):
            raise RuntimeError("boom")
    client = make_client(db_path, tmp_path, lambda p, m: Crashing([]))
    run_id = client.post("/api/runs", json={"question": "crash please"}).json()["run_id"]
    events = read_stream(client, run_id)
    assert events[-1]["type"] == "server_error" and "boom" in events[-1]["message"]
    assert client.get("/api/config").json()["busy"] is False


# --- security (WEB-6..WEB-8) ---------------------------------------------------------------


def test_cross_origin_post_is_refused(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    resp = client.post("/api/runs", json={"question": "evil"}, headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_non_json_post_is_refused(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    resp = client.post("/api/runs", content='{"question": "form post"}', headers={"Content-Type": "text/plain"})
    assert resp.status_code == 415


def test_unexpected_host_header_is_refused(db_path, tmp_path):
    app = create_app(db_path=db_path, default_provider="ollama", runs_dir=tmp_path / "runs",
                     llm_factory=scripted_factory(), include_search=False)
    assert TestClient(app, base_url="http://attacker.example").get("/").status_code == 400


@pytest.mark.parametrize("run_id", ["..%2F..%2Fetc", "nope", "a" * 100])
def test_unknown_or_malformed_run_ids_404(db_path, tmp_path, run_id):
    client = make_client(db_path, tmp_path, scripted_factory())
    assert client.get(f"/api/runs/{run_id}/events").status_code == 404


def test_request_limits_are_validated(db_path, tmp_path):
    client = make_client(db_path, tmp_path, scripted_factory(happy_script()))
    assert client.post("/api/runs", json={"question": "ok question", "max_steps": 500}).status_code == 422
    assert client.post("/api/runs", json={"question": "x"}).status_code == 422
