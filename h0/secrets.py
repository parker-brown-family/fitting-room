"""Credential lookup. Keys are read here and nowhere else, and never logged."""

from __future__ import annotations

import os
import subprocess

_cache: dict[str, str | None] = {}

KEYRING = {
    "anthropic": ("harness-0", "anthropic-api"),
}


def get(name: str) -> str | None:
    """Return a credential: environment first, then the GNOME keyring."""
    if name in _cache:
        return _cache[name]
    env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(name)
    value = os.environ.get(env) if env else None
    if not value and name in KEYRING:
        service, account = KEYRING[name]
        try:
            r = subprocess.run(
                ["secret-tool", "lookup", "service", service, "account", account],
                capture_output=True, timeout=15, check=False,
            )
            value = r.stdout.decode().strip() or None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            value = None
    _cache[name] = value
    return value


def known_values() -> list[str]:
    """Every credential this process has loaded, for exact-match redaction."""
    return [v for v in _cache.values() if v]
