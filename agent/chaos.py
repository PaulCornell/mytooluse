"""Fault injection for tools (PRD 003, REL-14..REL-16).

With probability p, a tool attempt fails with a transient error *before* the tool runs.
The registry's retry logic should absorb these; the trace marks them injected=True.
"""

from __future__ import annotations

import random
import threading

from agent.errors import ToolTimeoutError, TransientToolError

_FAILURES = [
    ("503 Service Unavailable (injected)", TransientToolError),
    ("429 Too Many Requests (injected)", TransientToolError),
    ("connection reset by peer (injected)", TransientToolError),
    ("timed out waiting for response (injected)", ToolTimeoutError),
]


class Chaos:
    def __init__(self, probability: float, seed: int | None = None):
        if not 0.0 <= probability <= 1.0:
            raise ValueError("chaos probability must be between 0 and 1")
        self.probability = probability
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def maybe_fail(self, tool_name: str) -> None:
        with self._lock:
            if self._rng.random() >= self.probability:
                return
            message, error_cls = self._rng.choice(_FAILURES)
        raise error_cls(f"{tool_name}: {message}", injected=True)
