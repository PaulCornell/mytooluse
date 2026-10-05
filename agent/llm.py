"""Model clients behind a small protocol, so providers and replay are interchangeable (LOOP-7).

Two providers speak the same Anthropic Messages API, so the loop, tools, traces and replay
are identical for both:

- "ollama"    (default): a local open-weight model served by Ollama. No account, no cost.
- "anthropic": Claude via the Anthropic API. Needs ANTHROPIC_API_KEY; billed per token.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

import anthropic
import httpx
from anthropic.types.beta import BetaMessage

PROVIDERS = ("ollama", "anthropic")
DEFAULT_PROVIDER = os.environ.get("ASKDATA_PROVIDER", "ollama")
DEFAULT_MODELS = {"ollama": "askdata-local", "anthropic": "claude-sonnet-5-5"}
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MIN_LOCAL_CONTEXT = 12_000  # system prompt + tools + schema + a few results

# Claude models that take adaptive thinking + effort, and those with server-side refusal fallback.
_ADAPTIVE_MODELS = {
    "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5",
}
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class LLM(Protocol):
    provider: str
    model: str
    context_window: int | None  # known prompt limit, if the provider truncates silently

    def create(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None = None, timeout: float | None = None,
    ) -> BetaMessage: ...


class AnthropicLLM:
    """Claude via the Messages API. No retries here: the loop owns them (REL-1)."""

    provider = "anthropic"
    context_window = None

    def __init__(
        self, model: str = DEFAULT_MODELS["anthropic"], *, max_tokens: int = 16_000, effort: str = "medium",
        client: anthropic.Anthropic | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.client = client or anthropic.Anthropic(max_retries=0, timeout=180.0)

    def create(self, *, system, messages, tools, tool_choice=None, timeout=None) -> BetaMessage:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "cache_control": {"type": "ephemeral"},  # automatic prompt caching (LOOP-9)
        }
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if timeout is not None:
            kwargs["timeout"] = timeout
        if self.model in _ADAPTIVE_MODELS:
            # "summarized" makes the reasoning visible in traces; the default would be empty text.
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
            kwargs["output_config"] = {"effort": self.effort}
        if self.model in _FALLBACK_MODELS:
            # LOOP-8: on a classifier refusal, the API re-runs the request on a recommended model.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return self.client.beta.messages.create(**kwargs)


class LocalModelError(RuntimeError):
    """The local model can't be used as configured. The message says how to fix it."""


class OllamaLLM:
    """A local model through Ollama's Anthropic-compatible /v1/messages endpoint (PRD 006).

    Only the portable core of the API is sent: no thinking, effort, caching or fallback
    parameters, which are Claude features.
    """

    provider = "ollama"

    def __init__(
        self, model: str = DEFAULT_MODELS["ollama"], *, host: str = OLLAMA_HOST, max_tokens: int = 4096,
        client: anthropic.Anthropic | None = None, http: httpx.Client | None = None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.max_tokens = max_tokens
        self.context_window: int | None = None
        # Ollama ignores the key, but the SDK requires one. Local generation can be slow.
        self.client = client or anthropic.Anthropic(base_url=self.host, api_key="ollama", max_retries=0, timeout=600.0)
        self.http = http or httpx.Client(timeout=10.0)

    def create(self, *, system, messages, tools, tool_choice=None, timeout=None) -> BetaMessage:
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": self.max_tokens, "system": system,
            "messages": messages, "tools": tools,
        }
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self.client.beta.messages.create(**kwargs)

    def preflight(self) -> list[str]:
        """Check that Ollama is running and the model can do this job (LOCAL-3, LOCAL-4).

        Raises LocalModelError for problems that would make every run fail; returns warnings
        for problems that would make runs worse.
        """
        try:
            resp = self.http.post(f"{self.host}/api/show", json={"model": self.model})
        except httpx.TransportError as exc:
            raise LocalModelError(
                f"Ollama isn't reachable at {self.host} ({exc}). Install it from https://ollama.com and "
                "start it (open the app, or run `ollama serve`)."
            ) from exc
        if resp.status_code == 404:
            hint = ("Run `askdata setup-local` to create it." if self.model == DEFAULT_MODELS["ollama"]
                    else f"Run `ollama pull {self.model}`.")
            raise LocalModelError(f"Model '{self.model}' isn't installed in Ollama. {hint}")
        resp.raise_for_status()
        info = resp.json()

        if "tools" not in info.get("capabilities", ["tools"]):
            raise LocalModelError(
                f"Model '{self.model}' doesn't support tool calling, which this agent needs. "
                "Use a tool-capable model such as qwen2.5 or qwen3."
            )

        warnings = []
        self.context_window = _num_ctx(info.get("parameters", ""))
        if self.context_window is None:
            warnings.append(
                f"'{self.model}' has no num_ctx set, so Ollama uses its server default (often 4,096 tokens) and "
                "silently drops the start of long conversations. Run `askdata setup-local` to create "
                "'askdata-local' with a 16k context, or set OLLAMA_CONTEXT_LENGTH before starting Ollama."
            )
        elif self.context_window < MIN_LOCAL_CONTEXT:
            warnings.append(f"'{self.model}' has a {self.context_window:,}-token context; "
                            f"at least {MIN_LOCAL_CONTEXT:,} is recommended.")
        return warnings


def _num_ctx(parameters: str) -> int | None:
    for line in parameters.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "num_ctx" and parts[1].isdigit():
            return int(parts[1])
    return None


def make_llm(provider: str, model: str | None = None, *, effort: str = "medium") -> AnthropicLLM | OllamaLLM:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    model = model or DEFAULT_MODELS[provider]
    if provider == "anthropic":
        return AnthropicLLM(model, effort=effort)
    return OllamaLLM(model)
