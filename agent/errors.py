"""Error taxonomy (PRD 003 §2).

Every failure in the system is one of three kinds, and the kind decides who handles it:

- TransientError      -> the harness retries with backoff; the model never sees it
                         unless retries are exhausted.
- ModelFixableError   -> returned to the model as a tool_result with is_error=True
                         and an actionable message, so it can correct itself.
- FatalError          -> the run stops with a status.
"""


class AgentError(Exception):
    """Base class for all errors raised by the harness or its tools."""

    kind = "internal"


# --- transient: retried by the harness -------------------------------------


class TransientError(AgentError):
    kind = "transient"

    def __init__(self, message: str, *, retry_after: float | None = None, injected: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.injected = injected


class TransientToolError(TransientError):
    """A tool's backing service is temporarily unavailable (HTTP 429/5xx, network)."""


class ToolTimeoutError(TransientToolError):
    """A tool's backing service did not respond in time."""


# --- model-fixable: sent back to the model as is_error ----------------------


class ModelFixableError(AgentError):
    kind = "fixable"


class ToolInputError(ModelFixableError):
    """The tool input failed schema validation, or the tool name is unknown."""

    kind = "invalid_input"


class ToolExecutionError(ModelFixableError):
    """The tool ran and failed in a way the model can fix (bad SQL, a Python exception)."""

    kind = "execution_error"


# --- fatal: ends the run ----------------------------------------------------


class FatalError(AgentError):
    kind = "fatal"


class WallClockExceeded(FatalError):
    """The run's wall-clock budget ran out before or during a model call (REL-17)."""
