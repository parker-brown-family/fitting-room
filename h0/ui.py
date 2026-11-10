"""Terminal rendering. Plain ANSI, line-oriented, so Terminal Delight's affordances fire:
commands are shown whole and line-initial, never elided."""

from __future__ import annotations

import json
import os
import sys

DIM, BOLD, RED, GREEN, YELLOW, CYAN, RESET = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"


def _summary(name: str, args: dict) -> str:
    if name == "bash":
        return args.get("command", "")
    if name in ("read", "write", "edit"):
        return args.get("path", "")
    if name == "grep":
        return f"/{args.get('pattern', '')}/ {args.get('path', '')}".rstrip()
    if name == "glob":
        return args.get("pattern", "")
    if name == "check":
        return args.get("name", "")
    return json.dumps(args)[:200]


class UI:
    def __init__(self, quiet: bool = False, show_thinking: bool = False, color: bool | None = None, stream=None):
        self.out = stream or sys.stderr if quiet else (stream or sys.stdout)
        self.quiet = quiet
        self.show_thinking = show_thinking
        self.color = (os.isatty(1) and not os.environ.get("NO_COLOR")) if color is None else color
        self._mid_line = False
        self._thinking_open = False

    def _c(self, code: str, s: str) -> str:
        return f"{code}{s}{RESET}" if self.color else s

    def _w(self, s: str) -> None:
        if not self.quiet:
            self.out.write(s)
            self.out.flush()

    def text(self, s: str) -> None:
        if self._thinking_open:
            self._w("\n")
            self._thinking_open = False
        self._w(s)
        self._mid_line = not s.endswith("\n")

    def thinking(self, s: str) -> None:
        if not self.show_thinking:
            return
        if not self._thinking_open:
            self._w(self._c(DIM, "\n✻ "))
            self._thinking_open = True
        self._w(self._c(DIM, s))

    def _newline(self) -> None:
        if self._mid_line or self._thinking_open:
            self._w("\n")
            self._mid_line = self._thinking_open = False

    def tool_start(self, name: str, args: dict) -> None:
        self._newline()
        self._w(self._c(CYAN, f"⏺ {name}") + f"  {_summary(name, args)}\n")

    def tool_end(self, name: str, res, ms: int) -> None:
        mark = self._c(GREEN, "✓") if res.ok else self._c(RED, "✗")
        lines = res.text.count("\n") + 1 if res.text else 0
        extra = ""
        if res.meta.get("denied"):
            extra = self._c(YELLOW, f" refused: {res.meta['denied']}")
        elif res.meta.get("redactions"):
            extra = self._c(YELLOW, f" {len(res.meta['redactions'])} redacted")
        self._w(self._c(DIM, f"  {mark} {ms} ms · {lines} lines") + extra + "\n")

    def note(self, s: str) -> None:
        self._newline()
        self._w(self._c(YELLOW, f"· {s}") + "\n")

    def turn_end(self, totals) -> None:
        pass

    def ask(self, what: str, decision) -> bool | str:
        if self.quiet or not sys.stdin.isatty():
            return False
        self._newline()
        self._w(self._c(YELLOW, f"? {what}\n  {decision.reason}\n  allow? [y]es / [N]o / [a]lways this session: "))
        try:
            ans = input().strip().lower()
        except EOFError:
            return False
        if ans in ("a", "always"):
            return "always"
        return ans in ("y", "yes")
