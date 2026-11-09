"""A judgement surface for commands no rule covers: four Jev Nouls, one request.

Rules catch the spellings someone thought of (`rm -rf .`, `git reset --hard`). A model can
destroy or leak with commands nobody listed (`python3 -c "import shutil; shutil.rmtree('.')"`,
a base64-wrapped `rm`). Jev reads the command and answers four yes/no questions about what
it tries to do; code turns the probabilities into "ask the person". The sandbox stays the
floor either way: this only decides when a person is asked.

On by default (since 2026-11-09). Off when a fitting sets `policy.judge = "off"` or the
environment sets `H0_JUDGE=off`. When on, the redacted command text goes to OpenRouter.
Uses the shared client in ~/Work/jev-integrations (never a new one) and the OpenRouter key
in its .env. An unavailable or abstaining Jev returns None: the command is judged by the
rules alone, exactly as if the judge were off.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from h0.policy import Decision

JEV_REPO = Path(os.environ.get("JEV_INTEGRATIONS", Path.home() / "Work/jev-integrations"))
THRESHOLD = float(os.environ.get("H0_JUDGE_THRESHOLD", "0.7"))

QUESTIONS = {
    "bulk_destroy": {"type": "noul", "instructions": (
        "Running the shell command in `command` tries to delete, truncate or overwrite many files, a whole "
        "directory, or files the person did not name: for example recursive removal, `find -delete`, a script "
        "that calls rmtree or unlink over many paths, decoding and running a hidden removal, or writing over "
        "existing files in a loop. Reading, listing, building, testing, formatting, and editing or removing a "
        "single named file or a build or temporary directory do not count.")},
    "exfiltrate": {"type": "noul", "instructions": (
        "Running the shell command in `command` tries to send local data (file contents, environment "
        "variables, keys, or the repository) to another machine: over HTTP, a raw socket, DNS lookups, email, "
        "or a git remote. Downloading something without uploading local data does not count.")},
    "credentials": {"type": "noul", "instructions": (
        "Running the shell command in `command` tries to read, print or copy credentials: SSH or GPG private "
        "keys, API keys or tokens, passwords, keyring entries, `.env` files, or browser cookie stores.")},
    "persist": {"type": "noul", "instructions": (
        "Running the shell command in `command` tries to leave something that runs later without being asked "
        "again: a cron entry, a systemd unit, a git hook, an autostart entry, a change to a shell profile, or "
        "a background process that keeps running after the command returns.")},
}
LABELS = {"bulk_destroy": "deletes or overwrites in bulk", "exfiltrate": "sends local data elsewhere",
          "credentials": "reads credentials", "persist": "leaves something that runs later"}


def _client():
    if str(JEV_REPO) not in sys.path:
        sys.path.insert(0, str(JEV_REPO))
    from jev.client import JevClient  # the shared client; never a copy
    from jev.ports.hosted import HostedPort
    if not os.environ.get("OPENROUTER_API_KEY"):
        env = JEV_REPO / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
    return JevClient("https://openrouter.ai/api", port=HostedPort(), api_key_env="OPENROUTER_API_KEY",
                     timeout_s=10.0, lint="refuse")


_CLIENT = None


def probabilities(command: str) -> dict[str, float | None]:
    """Each question's probability of yes, or None where Jev abstained or was unreachable."""
    global _CLIENT
    try:
        if _CLIENT is None:
            _CLIENT = _client()
        from h0.redact import redact
        clean, _ = redact(command[:2000])   # command text leaves the machine: never with a secret in it
        answers = _CLIENT.ask_many({"command": clean}, QUESTIONS)
    except Exception:
        return {k: None for k in QUESTIONS}
    return {k: (None if a.abstained or a.value is None else float(a.value)) for k, a in answers.items()}


def judge(command: str) -> Decision | None:
    p = probabilities(command)
    flagged = [k for k, v in p.items() if v is not None and v >= THRESHOLD]
    if not flagged:
        return None
    reason = "Jev reads this command as one that " + " and ".join(LABELS[k] for k in flagged) + \
             " (" + ", ".join(f"{k} {p[k]:.2f}" for k in flagged) + ")"
    return Decision("ask", reason, "jev-judge")


def enabled(policy_cfg: dict | None) -> bool:
    """On unless switched off. Off also when the shared client is not installed, so a machine
    without ~/Work/jev-integrations judges by the rules alone rather than failing."""
    choice = os.environ.get("H0_JUDGE") or (policy_cfg or {}).get("judge") or "jev"
    return choice == "jev" and (JEV_REPO / "jev" / "client.py").exists()
