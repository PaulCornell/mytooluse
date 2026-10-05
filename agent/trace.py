"""JSONL trace log (PRD 004).

Each event is written and flushed immediately, so a crashed run still leaves a usable trace.
Events are also kept in memory (for replay comparison and tests) and fanned out to listeners
(for the CLI's live output).
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

Listener = Callable[[dict[str, Any]], None]


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Tracer:
    def __init__(self, run_id: str, path: Path | None = None, listeners: list[Listener] | None = None):
        self.run_id = run_id
        self.path = path
        self.events: list[dict[str, Any]] = []
        self.listeners = listeners or []
        self._seq = 0
        self._lock = threading.Lock()  # tools may run concurrently
        self._fh = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("w", encoding="utf-8")

    def emit(self, type: str, step: int, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "run_id": self.run_id,
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "step": step,
                "type": type,
                **fields,
            }
            self.events.append(event)
            if self._fh:
                self._fh.write(json.dumps(event, default=str) + "\n")
                self._fh.flush()
        for listener in self.listeners:
            listener(event)
        return event

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_trace(path: Path) -> list[dict[str, Any]]:
    return list(iter_trace(path))


def iter_trace(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)
