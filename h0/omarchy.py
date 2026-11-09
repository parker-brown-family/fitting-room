"""Omarchy-specific checks that do not need a model.

audit_theme: a theme folder can carry code. Hyprland requires a theme's hyprland.lua and
gum_env.lua at login and Neovim loads neovim.lua at startup; `omarchy theme install`
strips those from a git clone, but a folder copied in by hand keeps them. This scan reports
every file that would run, every network or credential reference in it, and any text
addressed to an AI assistant.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

RUNS_CODE = {"hyprland.lua": "Hyprland loads it at login", "gum_env.lua": "loaded at login",
             "neovim.lua": "Neovim runs it at startup", "vscode.json": "names a VS Code extension to install"}
NETWORK = re.compile(r"\b(curl|wget|nc|ncat|socat|ssh|scp|rsync)\b|https?://|/dev/tcp/")
CREDENTIAL = re.compile(r"\.ssh/|id_(rsa|ed25519|ecdsa)|\.gnupg|keyring|secret-tool|\.env\b|token|password", re.I)
EXEC = re.compile(r"\b(jobstart|vim\.fn\.system|os\.execute|io\.popen|launch_on_start|exec_on_start|exec_cmd|hl\.exec)\b|\|\s*(ba)?sh\b|base64")
TO_AGENTS = re.compile(r"(AI|LLM)\s+(assistant|agent)s?|ignore (all |any )?previous|do not (mention|tell)|for (the )?(AI|assistant)", re.I)


def audit_theme(path: Path) -> list[dict]:
    path = Path(path)
    findings: list[dict] = []
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = str(f.relative_to(path))
        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            continue
        mode = f.stat().st_mode
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        why = RUNS_CODE.get(f.name) or ("executable script" if mode & stat.S_IXUSR or f.suffix in {".sh", ".bash"} else
                                        "Lua code" if f.suffix == ".lua" else None)
        if why:
            findings.append({"file": rel, "kind": "runs-code", "why": why})
        for n, line in enumerate(text.splitlines(), 1):
            for kind, rx in (("network", NETWORK), ("credential", CREDENTIAL), ("exec", EXEC), ("addressed-to-ai", TO_AGENTS)):
                if rx.search(line) and (kind == "addressed-to-ai" or why):
                    findings.append({"file": rel, "line": n, "kind": kind, "text": line.strip()[:200]})
    return findings
