"""Paths, model facts and settings precedence (flag > env > fitting > default)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
STATE = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "h0"
DATA = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "h0"
REPO = Path(__file__).resolve().parent.parent

DEFAULT_MODEL = "claude-opus-5-5"

# USD per million tokens: input, 5-minute cache write, cache read, output.
# From platform.claude.com/docs/en/about-claude/pricing, read 2026-11-08.
PRICES = {
    "claude-fable-5-1": (10.0, 12.5, 0.25, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 0.10, 10.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-opus-5": (5.0, 6.25, 0.50, 25.0),
    "claude-sonnet-5": (3.0, 3.75, 0.30, 15.0),
}
HAIKU_LONG = (0.50, 0.625, 0.05, 2.50)  # Haiku 5.5 prompts over 100k tokens


def price(model: str, usage: dict) -> float | None:
    """Dollar cost of one response's usage, or None when the model has no known price."""
    p = PRICES.get(model)
    if p is None:
        return None
    prompt = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    if model == "claude-haiku-5-5" and prompt > 100_000:
        p = HAIKU_LONG
    return (
        usage.get("input_tokens", 0) * p[0]
        + usage.get("cache_creation_input_tokens", 0) * p[1]
        + usage.get("cache_read_input_tokens", 0) * p[2]
        + usage.get("output_tokens", 0) * p[3]
    ) / 1_000_000


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    provider: str = "anthropic"          # anthropic | openai | fake
    base_url: str | None = None          # for openai-compatible servers
    effort: str | None = None            # low | medium | high | xhigh | max; None = model default
    max_tokens: int = 64_000
    max_turns: int = 80
    budget_usd: float | None = None      # hard stop for one session; None = no cap
    finish_review: bool = False          # once, at the first finish: list the request's expectations as met/not met
    mode: str = "auto"                   # auto (ask on risky) | yolo (allow in sandbox) | plan (read-only)
    headless: bool = False               # no prompts: ask becomes deny unless yolo
    tool_output_chars: int = 16_000
    context_clear_at: int = 160_000      # estimated tokens before old tool results are cleared
    local: bool = False                  # local-model profile: tighter limits, fewer tools
    sim_prefill_tps: float = 300.0       # simulated local prefill speed, tokens/s
    sim_decode_tps: float = 16.0         # simulated local decode speed, tokens/s
    extra: dict = field(default_factory=dict)

    def apply_local_profile(self) -> None:
        self.local = True
        self.tool_output_chars = min(self.tool_output_chars, 6_000)
        self.context_clear_at = min(self.context_clear_at, 24_000)
        self.max_tokens = min(self.max_tokens, 8_192)


def ensure_dirs() -> None:
    for d in (STATE / "sessions", DATA / "fittings"):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
