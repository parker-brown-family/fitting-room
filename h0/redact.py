"""Secret detection. A secret that reaches the model can leave with the model's output,
so tool output is scrubbed before it enters the context.

Each match becomes [REDACTED:<kind>:<id>], where id is the first eight hex digits of an
HMAC of the value under a key chosen per process: stable within a session, so the model can
tell two redacted values apart or see that they are the same, and useless for confirming a
guessed value, because the model does not hold the key.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
from collections import Counter

from h0 import secrets

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)")),
    # a key body read without its header (from a line offset, or through grep or tail)
    ("key-material", re.compile(r"(?:^[A-Za-z0-9+/=]{60,80}\r?\n){3,}", re.M)),
    ("anthropic", re.compile(r"sk-ant-[a-z]+\d*-[A-Za-z0-9_\-]{20,}")),
    ("openrouter", re.compile(r"sk-or-v1-[A-Fa-f0-9]{32,}")),
    ("openai", re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("github", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}")),
    ("aws-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("db-url", re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)://[^:\s/@]+:[^@\s]+@[^\s'\"]+")),
]

# NAME = value, where NAME says secret and value looks like a literal secret, not code.
ASSIGNMENT = re.compile(
    r"(?im)^(?P<lead>[ \t]*(?:export[ \t]+)?[\"']?(?P<name>[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIAL)[A-Z0-9_]*)[\"']?[ \t]*[:=][ \t]*[\"']?)(?P<value>[^\s\"'#]{8,})"
)


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_literal(value: str) -> bool:
    if any(ch in value for ch in "()[]{}$<>`,;"):
        return False
    if value.lower() in {"changeme", "password", "placeholder", "redacted", "example"}:
        return False
    return len(value) >= 12 and _entropy(value) >= 3.0


_SESSION_KEY = os.urandom(32)


def _tag(kind: str, value: str) -> str:
    # keyed per process: stable within a session, but the model cannot confirm a guessed
    # value by hashing it itself
    return f"[REDACTED:{kind}:{hmac.new(_SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()[:8]}]"


def redact(text: str) -> tuple[str, list[str]]:
    """Return (clean_text, kinds_found)."""
    found: list[str] = []
    for value in secrets.known_values():
        if value and value in text:
            text = text.replace(value, _tag("harness-credential", value))
            found.append("harness-credential")
    for kind, pat in PATTERNS:
        def sub(m: re.Match, kind=kind) -> str:
            found.append(kind)
            return _tag(kind, m.group(0))
        text = pat.sub(sub, text)

    def sub_assign(m: re.Match) -> str:
        value = m.group("value")
        if value.startswith("[REDACTED:") or not _looks_literal(value):
            return m.group(0)
        found.append("assignment")
        return m.group("lead") + _tag("assignment", value)

    text = ASSIGNMENT.sub(sub_assign, text)
    return text, found


SECRET_PATH = re.compile(
    r"(?:^|/)(?:\.env(?:\.(?!example$|sample$|template$|dist$)[^/]+)?|\.envrc|\.netrc|\.pgpass|\.git-credentials|\.npmrc|\.pypirc"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?!\.pub$)[^/]*|[^/]*\.(?:pem|key|p12|pfx|jks|keystore)|credentials(?:\.json)?|secrets?\.(?:json|ya?ml|toml)"
    r"|service[-_]account[^/]*\.json)$"
)


def is_secret_path(path: str) -> bool:
    return bool(SECRET_PATH.search(path))
