"""The model port. Messages are kept in Anthropic's block format internally; other
providers translate at their edge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Turn:
    blocks: list[dict]
    stop_reason: str | None
    usage: dict = field(default_factory=dict)   # input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens, thinking_tokens
    usage_estimated: bool = False                # True when the server reported no usage and we counted characters
    model: str = ""
    served_model: str | None = None              # what the server says answered; None when it did not say
    ttft_ms: int | None = None
    total_ms: int = 0
    retries: int = 0


Delta = Callable[[str], None]


class Provider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[dict], settings,
                 on_text: Delta | None = None, on_thinking: Delta | None = None) -> Turn: ...


def make(settings) -> "Provider":
    if settings.provider == "anthropic":
        from h0 import secrets
        from h0.providers.anthropic import Anthropic
        key = secrets.get("anthropic")
        if not key:
            raise ProviderError("no Anthropic key: set ANTHROPIC_API_KEY or store one with "
                                "`secret-tool store --label=h0 service harness-0 account anthropic-api`")
        return Anthropic(key, settings.base_url)
    if settings.provider == "openai":
        from h0.providers.openai_compat import OpenAICompat
        return OpenAICompat(settings.base_url or "http://127.0.0.1:11434/v1")
    raise ProviderError(f"unknown provider {settings.provider!r}")
