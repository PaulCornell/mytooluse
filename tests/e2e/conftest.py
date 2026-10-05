"""Browser tests for the web app (PRD 007).

Each test starts the real FastAPI server on a free port, with a scripted model in place of
Ollama or Claude and fake provider statuses, then drives the page in headless Chromium.
No network, Ollama, or API key is needed.

Collected only when Playwright is installed (see tests/conftest.py):
    pip install -e '.[e2e]' && playwright install chromium
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from agent.web.server import create_app
from tests.conftest import ScriptedLLM

OLLAMA_OK = {"id": "ollama", "label": "Local (Ollama) · free", "model": "askdata-local",
             "available": True, "warnings": [], "message": None}
CLAUDE_OFF = {"id": "anthropic", "label": "Claude · paid", "model": "claude-sonnet-5-5", "available": False,
              "warnings": [], "message": "Set ANTHROPIC_API_KEY before starting the server to use Claude."}


@dataclass
class LiveApp:
    url: str
    runs_dir: Path
    scripts: list[Any] = field(default_factory=list)  # each item: a list of responses, or an LLM object
    statuses: list[dict[str, Any]] = field(default_factory=lambda: [OLLAMA_OK, CLAUDE_OFF])

    def queue(self, *items: Any) -> None:
        self.scripts.extend(items)

    def llm_factory(self, provider: str, model: str | None):
        item = self.scripts.pop(0)
        return ScriptedLLM(item) if isinstance(item, list) else item


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(db_path: Path, tmp_path: Path):
    port = _free_port()
    app_state = LiveApp(url=f"http://127.0.0.1:{port}", runs_dir=tmp_path / "runs")
    app = create_app(
        db_path=db_path, default_provider="ollama", runs_dir=app_state.runs_dir,
        llm_factory=app_state.llm_factory, include_search=False,
        provider_status=lambda provider, model: app_state.statuses,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server didn't start")
        time.sleep(0.02)
    yield app_state
    server.should_exit = True
    thread.join(timeout=5)
