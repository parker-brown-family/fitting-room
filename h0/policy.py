"""Permission policy.

Three layers decide every tool call, in order:
  1. hard rules   — paths and patterns that are never allowed (refuse, with a reason)
  2. rule of two  — a session may hold at most two of: untrusted input, sensitive data,
                    egress. Untrusted input is assumed (a repository can carry an
                    injected instruction), so sensitive data and egress exclude each other.
  3. risk rules   — risky but legitimate actions (destructive git, deleting trees) ask
                    the person; headless sessions treat ask as deny unless --yolo.
An optional judge (for example a Jev Noul) can be consulted for commands no rule covers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from h0.redact import is_secret_path

HOME = Path.home()


@dataclass
class Decision:
    verdict: str          # allow | ask | deny
    reason: str = ""
    rule: str = ""
    key: str = ""         # what a person approves when they answer an ask; empty for allow and deny

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


@dataclass
class SessionFlags:
    untrusted: bool = True
    sensitive: bool = False
    egress: bool = False
    attempted_egress: int = 0   # commands that tried the network from a no-network sandbox

    def as_dict(self) -> dict:
        return {"untrusted": self.untrusted, "sensitive": self.sensitive, "egress": self.egress,
                "attempted_egress": self.attempted_egress}


DESTRUCTIVE = [
    (re.compile(r"\brm\s+(?:(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--force|-f|--)\s+)+(?:\S+\s+)*?(?:/|\.|\./|\*|~|~/|\$HOME|\.\.)(?:\s|$)"), "removes a whole tree"),
    (re.compile(r"\bfind\b[^|;&]*\s-delete\b"), "deletes files in bulk"),
    (re.compile(r"\bgit\s+(?:reset\s+--hard|clean\b[^|;&]*\s-[a-zA-Z]*f|checkout\s+(?:--\s+)?\.(?:\s|$)|restore\s+\.|stash\s+(?:drop|clear)|branch\s+-D|update-ref\s+-d|reflog\s+expire|gc\b[^|;&]*--prune)"), "discards work in git"),
    (re.compile(r"\bgit\s+push\b.*(?:--force|\s-f\b|--delete|\s\+\S+|\s:\S+)"), "rewrites or deletes a remote branch"),
    (re.compile(r"\b(?:mkfs|wipefs|shred)\b|\bdd\s+[^|]*\bof=/dev/"), "writes a device"),
]
EGRESS_HINT = re.compile(
    r"\b(?:curl|wget|nc|ncat|socat|ssh|scp|sftp|rsync|telnet|ftp)\b|\bgit\s+(?:push|fetch|pull|clone|ls-remote)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:install|add|i|publish)\b|\bpip3?\s+install\b|\bcargo\s+(?:install|publish|update)\b"
    r"|/dev/tcp/|\bpython3?\s+-m\s+http\.server\b"
)
CREDENTIAL_HINT = re.compile(
    r"\bsecret-tool\b|\bgh\s+auth\s+token\b|\bgit\s+credential\b|\bprintenv\b|\benv\b\s*(?:\||$)|/proc/[^/\s]+/environ"
    r"|\.ssh/|\.gnupg|\.config/gh/|keyring|\bpass\s+show\b|credentials(?:\.toml|\.json)?\b|\.git-credentials|\.netrc|\.env\b"
)
PROTECTED_WRITE = re.compile(r"(?:^|/)\.git(?:/|$)|(?:^|/)\.git/hooks/|(?:^|/)\.envrc$")


@dataclass
class Policy:
    root: Path
    read_roots: list[Path] = field(default_factory=list)
    write_roots: list[Path] = field(default_factory=list)
    allow_secret_paths: list[str] = field(default_factory=list)
    mode: str = "auto"
    headless: bool = False
    flags: SessionFlags = field(default_factory=SessionFlags)
    judge: Callable[[str], Decision | None] | None = None
    approvals: set[str] = field(default_factory=set)
    protected: list[Path] = field(default_factory=list)   # never writable: e.g. the fittings that define this policy
    home: Path | None = None   # what `~` means to the model: the sandbox's HOME when a run gives it one

    def __post_init__(self) -> None:
        self.root = Path(os.path.realpath(self.root))

    # -- helpers --------------------------------------------------------------
    def resolve(self, path: str, cwd: Path | None = None) -> Path:
        # `~` must name the same place for the file tools as for bash, or a path the model
        # just listed with `ls ~/.config` is refused as outside the project by `edit`
        if self.home is not None and (path == "~" or path.startswith("~/")):
            path = str(self.home) + path[1:]
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = (cwd or self.root) / p
        return Path(os.path.realpath(p))

    def _under(self, p: Path, roots: list[Path]) -> bool:
        for r in roots:
            r = Path(os.path.realpath(r))
            if p == r or str(p).startswith(str(r) + os.sep):
                return True
        return False

    def _finish(self, d: Decision, key: str, yolo_approves: bool = True) -> Decision:
        if d.verdict == "ask":
            if key in self.approvals or (self.mode == "yolo" and yolo_approves):
                return Decision("allow", d.reason + " (approved)", d.rule)
            if self.headless:
                return Decision("deny", d.reason + " (headless: no one to ask)", d.rule)
            return Decision("ask", d.reason, d.rule, key)
        return d

    # -- reads ----------------------------------------------------------------
    def check_read(self, p: Path) -> Decision:
        sp = str(p)
        if is_secret_path(sp) and not any(sp.endswith(a) for a in self.allow_secret_paths):
            return Decision("deny", f"{p.name} looks like a credential file; ask the person to share what you need from it", "secret-path")
        if self._under(p, [self.root] + self.read_roots):
            return Decision("allow", rule="inside-project")
        if self._under(p, [HOME]):
            return Decision("deny", "outside the project, inside the home directory", "home-outside-project")
        if self._under(p, [Path("/proc"), Path("/sys"), Path("/run"), Path("/dev"), Path("/tmp"), Path("/var/tmp"),
                           Path("/mnt"), Path("/media")]):
            return Decision("deny", "runtime, temporary and removable-media trees are not readable (the sandbox hides them too)", "system-runtime")
        return Decision("allow", rule="system-path")

    # -- writes ---------------------------------------------------------------
    def check_write(self, p: Path) -> Decision:
        if self.mode == "plan":
            return Decision("deny", "plan mode is read-only", "plan-mode")
        sp = str(p)
        rel = sp[len(str(self.root)) + 1:] if sp.startswith(str(self.root) + os.sep) else sp
        if self._under(p, self.protected):
            return Decision("deny", "this file configures the harness's own policy", "protected-config")
        if PROTECTED_WRITE.search(rel):
            return Decision("deny", "git internals and .envrc run code outside the sandbox; change them through git or by hand", "protected-path")
        if is_secret_path(sp):
            return Decision("deny", "writing a credential-shaped file", "secret-path")
        if self._under(p, [self.root] + self.write_roots):
            return Decision("allow", rule="inside-project")
        # --yolo approves what runs inside the sandbox. The file tools run in h0's own process, so a
        # yolo approval here was a write anywhere the person can write: on 2026-11-10 a test session
        # whose home had been moved edited the real ~/.config/hypr through a path quoted in a config
        # file. Only a person, one write at a time, may approve this.
        return self._finish(Decision("ask", f"write outside the project: {p}", "write-outside"), f"write:{p}",
                            yolo_approves=False)

    # -- commands -------------------------------------------------------------
    def check_exec(self, command: str, net: bool = False) -> Decision:
        if self.mode == "plan":
            return Decision("deny", "plan mode runs no commands", "plan-mode")
        if CREDENTIAL_HINT.search(command):
            if self.flags.egress:
                return Decision("deny", "credential access in a session that has egress breaks the rule of two", "rule-of-two")
            self.flags.sensitive = True
        for pat, why in DESTRUCTIVE:  # before the network branch, so a force-push is not asked about as "network"
            if pat.search(command):
                d = self._finish(Decision("ask", why, "destructive"), f"exec:{command}")
                if not d.allowed:
                    return d
        if net:
            if self.flags.sensitive:
                return Decision("deny", "this session has touched sensitive data, so it may not reach the network (rule of two)", "rule-of-two")
            d = self._finish(Decision("ask", "command wants network access", "egress"), f"net:{command}")
            if d.allowed:
                self.flags.egress = True
            return d
        if self.judge:
            verdict = self.judge(command)
            if verdict is not None:
                return self._finish(verdict, f"exec:{command}")
        return Decision("allow", rule="sandboxed")

    def note_sensitive(self) -> None:
        self.flags.sensitive = True

    def approve(self, key: str) -> None:
        self.approvals.add(key)
