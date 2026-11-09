"""The model's tools. Six by default, plus `check` when a fitting names checks.

Every result passes through redaction and truncation before it reaches the model.
File tools run in-process behind the path policy; shell commands run in the sandbox.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from h0 import redact
from h0.policy import EGRESS_HINT, Decision, Policy
from h0.sandbox import Sandbox

SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".next", ".venv", "venv", "__pycache__",
             ".cache", ".turbo", ".svelte-kit", "coverage", ".pytest_cache", ".mypy_cache", ".gradle"}
NET_FAIL = re.compile(r"Could not resolve|Network is unreachable|Temporary failure in name resolution|"
                      r"Name or service not known|getaddrinfo|ENOTFOUND|EAI_AGAIN|ENETUNREACH|ECONNREFUSED|"
                      r"Failed to connect|Could not connect|Connection refused|connect: |unable to access", re.I)


@dataclass
class ToolResult:
    ok: bool
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    risk: str
    handler: Callable[[dict], ToolResult]

    def api(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def truncate(text: str, limit: int) -> tuple[str, int]:
    if len(text) <= limit:
        return text, 0
    head = int(limit * 0.6)
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n… [{cut} characters omitted] …\n{text[-tail:]}", cut


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


class Toolbox:
    def __init__(self, root: Path, policy: Policy, sandbox: Sandbox, output_chars: int = 16_000,
                 checks: dict | None = None, ask: Callable[[str, Decision], bool] | None = None,
                 emit: Callable[..., None] | None = None, enabled: list[str] | None = None, local: bool = False):
        self.root = Path(os.path.realpath(root))
        self.cwd = self.root
        self.policy = policy
        self.sandbox = sandbox
        self.output_chars = output_chars
        self.checks = checks or {}
        self.ask = ask
        self.emit = emit or (lambda *a, **k: None)
        self.seen: set[Path] = set()
        self.local = local
        all_specs = self._all_specs()
        names = enabled or [s.name for s in all_specs]
        self.specs = [s for s in all_specs if s.name in names and (s.name != "check" or self.checks)]
        self.by_name = {s.name: s for s in self.specs}

    # -- registry ---------------------------------------------------------------
    def _all_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec("read", "Read a file (numbered lines) or list a directory. Use offset/limit for long files.",
                     _obj({"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
                           "limit": {"type": "integer", "minimum": 1}}, ["path"]), "read", self.read),
            ToolSpec("edit", "Replace an exact, unique piece of text in a file. Include enough surrounding text to be unique, or set replace_all.",
                     _obj({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
                           "replace_all": {"type": "boolean"}}, ["path", "old", "new"]), "write", self.edit),
            ToolSpec("write", "Create a file, or replace a whole file you have already read.",
                     _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), "write", self.write),
            ToolSpec("grep", "Search file contents with a Python regex. Returns path:line: text.",
                     _obj({"pattern": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"},
                           "ignore_case": {"type": "boolean"}, "max_results": {"type": "integer", "minimum": 1}}, ["pattern"]), "read", self.grep),
            ToolSpec("glob", "List files matching a glob such as src/**/*.rs, newest first.",
                     _obj({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]), "read", self.glob),
            ToolSpec("bash", "Run a bash command in the project sandbox: project writable, home hidden, no network. Each call starts in the project root; use cd within the command.",
                     _obj({"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 1800},
                           "net": {"type": "boolean", "description": "request network access; needs approval"}}, ["command"]), "exec", self.bash),
        ]
        if self.checks:
            names = sorted(self.checks)
            desc = "Run one of this project's named checks: " + "; ".join(f"{n} = {self.checks[n].get('about') or self.checks[n]['run']}" for n in names)
            specs.append(ToolSpec("check", desc, _obj({"name": {"type": "string", "enum": names}}, ["name"]), "exec", self.check))
        return specs

    def api(self) -> list[dict]:
        return [s.api() for s in self.specs]

    def call(self, name: str, args: dict) -> ToolResult:
        spec = self.by_name.get(name)
        if spec is None:
            return ToolResult(False, f"unknown tool {name!r}; available: {', '.join(self.by_name)}")
        try:
            result = spec.handler(args)
        except Exception as e:  # a tool bug must not end the session
            result = ToolResult(False, f"{type(e).__name__}: {e}")
        clean, kinds = redact.redact(result.text)
        if kinds:
            self.policy.note_sensitive()
            result.meta["redactions"] = kinds
            self.emit("redaction", tool=name, kinds=sorted(set(kinds)), count=len(kinds))
        clean, cut = truncate(clean, self.output_chars)
        if cut:
            result.meta["truncated_chars"] = cut
        result.text = clean
        return result

    # -- policy glue ------------------------------------------------------------
    def _gate(self, d: Decision, what: str) -> ToolResult | None:
        if d.verdict == "allow":
            return None
        if d.verdict == "ask" and self.ask and self.ask(what, d):
            if d.rule == "egress":  # an approved network grant counts toward the rule of two
                self.policy.flags.egress = True
            return None
        self.emit("policy_denied", what=what, rule=d.rule, reason=d.reason)
        return ToolResult(False, f"refused by policy ({d.rule}): {d.reason}", {"denied": d.rule})

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    # -- handlers ---------------------------------------------------------------
    def read(self, a: dict) -> ToolResult:
        p = self.policy.resolve(a["path"], self.cwd)
        if (r := self._gate(self.policy.check_read(p), f"read {p}")):
            return r
        if p.is_dir():
            entries = sorted(os.listdir(p))
            shown = [e + ("/" if (p / e).is_dir() else "") for e in entries[:300]]
            more = f"\n… {len(entries) - 300} more" if len(entries) > 300 else ""
            return ToolResult(True, "\n".join(shown) + more)
        if not p.exists():
            return ToolResult(False, f"no such file: {self._rel(p)}")
        data = p.read_bytes()[:3_000_000]
        if b"\0" in data[:8192]:
            return ToolResult(True, f"binary file, {p.stat().st_size} bytes")
        self.seen.add(p)
        lines = data.decode("utf-8", errors="replace").splitlines()
        offset = max(1, int(a.get("offset") or 1))
        limit = int(a.get("limit") or (250 if self.local else 600))
        chunk = lines[offset - 1: offset - 1 + limit]
        out = "\n".join(f"{i}\t{line[:2000]}" for i, line in enumerate(chunk, offset))
        rest = len(lines) - (offset - 1 + len(chunk))
        if rest > 0:
            out += f"\n… {rest} more lines; read with offset={offset + len(chunk)}"
        if not lines:
            out = "(empty file)"
        return ToolResult(True, out)

    def _write_atomic(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = p.stat().st_mode & 0o7777 if p.exists() else 0o644
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".h0-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, p)

    def write(self, a: dict) -> ToolResult:
        p = self.policy.resolve(a["path"], self.cwd)
        if (r := self._gate(self.policy.check_write(p), f"write {p}")):
            return r
        if p.exists() and p not in self.seen:
            return ToolResult(False, f"{self._rel(p)} exists; read it before replacing it, or use edit")
        self._write_atomic(p, a["content"])
        self.seen.add(p)
        self.emit("file_write", path=self._rel(p), bytes=len(a["content"].encode()))
        return ToolResult(True, f"wrote {len(a['content'].encode())} bytes to {self._rel(p)}")

    def edit(self, a: dict) -> ToolResult:
        p = self.policy.resolve(a["path"], self.cwd)
        if (r := self._gate(self.policy.check_write(p), f"edit {p}")):
            return r
        if not p.exists():
            return ToolResult(False, f"no such file: {self._rel(p)}; use write to create it")
        text = p.read_text(encoding="utf-8", errors="replace")
        old, new = a["old"], a["new"]
        if old == new:
            return ToolResult(False, "old and new are identical")
        n = text.count(old)
        if n == 0:
            hint = ""
            first = old.strip().splitlines()[0] if old.strip() else ""
            if first and first in text:
                hint = " The first line exists, so whitespace or a later line differs; read the region again."
            return ToolResult(False, f"old text not found in {self._rel(p)}.{hint}")
        if n > 1 and not a.get("replace_all"):
            return ToolResult(False, f"old text appears {n} times in {self._rel(p)}; add surrounding text or set replace_all")
        updated = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
        self._write_atomic(p, updated)
        self.seen.add(p)
        at = updated.find(new)
        line = updated.count("\n", 0, at) + 1 if at >= 0 else None
        self.emit("file_edit", path=self._rel(p), replacements=n if a.get("replace_all") else 1, line=line)
        return ToolResult(True, f"edited {self._rel(p)}" + (f" at line {line}" if line else "") + (f" ({n} replacements)" if a.get("replace_all") else ""))

    def _walk(self, base: Path, cap: int = 200_000):
        """Every file under base that the policy lets this session read, judged per file after
        resolving symlinks (the starting directory being readable says nothing about what is
        below it: /home is a system path, /home/<user> is not)."""
        n = 0
        cands = [base] if base.is_file() else None
        if cands is None:
            cands = []
            for dirpath, dirnames, filenames in os.walk(base):
                keep = []
                for d in dirnames:
                    if d in SKIP_DIRS or d.startswith(".h0-"):
                        continue
                    if not self.policy.check_read(Path(os.path.realpath(Path(dirpath) / d))).allowed and not self._is_ancestor_of_root(Path(dirpath) / d):
                        continue
                    keep.append(d)
                dirnames[:] = keep
                for f in filenames:
                    n += 1
                    if n > cap:
                        return
                    yield from self._readable(Path(dirpath) / f)
            return
        for c in cands:
            yield from self._readable(c)

    def _is_ancestor_of_root(self, p: Path) -> bool:
        rp = os.path.realpath(p)
        return str(self.root).startswith(rp + os.sep)

    def _readable(self, f: Path):
        real = Path(os.path.realpath(f))
        if self.policy.check_read(real).allowed and not redact.is_secret_path(str(real)):
            yield f

    def grep(self, a: dict) -> ToolResult:
        base = self.policy.resolve(a.get("path") or ".", self.cwd)
        if (r := self._gate(self.policy.check_read(base), f"grep {base}")):
            return r
        try:
            rx = re.compile(a["pattern"], re.I if a.get("ignore_case") else 0)
        except re.error as e:
            return ToolResult(False, f"bad regex: {e}")
        limit = int(a.get("max_results") or (40 if self.local else 120))
        glob = a.get("glob")
        hits: list[str] = []
        files = 0
        for f in self._walk(base):
            if glob and not fnmatch.fnmatch(f.name, glob) and not fnmatch.fnmatch(self._rel(f), glob):
                continue
            try:
                if f.stat().st_size > 1_500_000:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            files += 1
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self._rel(f)}:{i}: {line.strip()[:240]}")
                    if len(hits) >= limit:
                        return ToolResult(True, "\n".join(hits) + f"\n… stopped at {limit} results; narrow the pattern or path")
        return ToolResult(True, "\n".join(hits) if hits else f"no matches in {files} files")

    def glob(self, a: dict) -> ToolResult:
        base = self.policy.resolve(a.get("path") or ".", self.cwd)
        if (r := self._gate(self.policy.check_read(base), f"glob {base}")):
            return r
        if ".." in Path(a["pattern"]).parts:
            return ToolResult(False, "glob patterns may not climb with '..'; pass a different path instead")
        out = []
        for p in base.glob(a["pattern"]):
            try:
                parts = set(p.relative_to(base).parts)
            except ValueError:
                continue
            if parts & SKIP_DIRS:
                continue
            if p.is_file() and self.policy.check_read(Path(os.path.realpath(p))).allowed:
                out.append(p)
            if len(out) > 2000:
                break
        out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        lines = [self._rel(p) for p in out[:200]]
        more = f"\n… {len(out) - 200} more" if len(out) > 200 else ""
        return ToolResult(True, ("\n".join(lines) + more) if lines else "no files match")

    def _run(self, script: str, cwd: Path, net: bool, timeout: float, label: str) -> ToolResult:
        res = self.sandbox.run(script, cwd=cwd, net=net, timeout=timeout)
        status = "timed out" if res.timed_out else f"exit {res.exit}"
        meta = {"exit": res.exit, "ms": res.ms, "timed_out": res.timed_out}
        if not net and EGRESS_HINT.search(script) and NET_FAIL.search(res.output):
            self.policy.flags.attempted_egress += 1
            self.emit("egress_attempt", command=script[:500], label=label)
            meta["egress_blocked"] = True
        body = res.output.rstrip()
        if res.truncated_bytes:
            body += f"\n… [{res.truncated_bytes} bytes of output dropped]"
        if res.integrity:
            meta["integrity"] = res.integrity
            self.emit("integrity_violation", command=script[:500], files=res.integrity)
            body += ("\n[harness] This command created or changed " + ", ".join(res.integrity) +
                     ", which would run code outside the sandbox; the harness put them back.")
        return ToolResult(res.ok, f"[{status}, {res.ms} ms]\n{body}" if body else f"[{status}, {res.ms} ms]", meta)

    def bash(self, a: dict) -> ToolResult:
        cmd = a["command"]
        net = bool(a.get("net"))
        if (r := self._gate(self.policy.check_exec(cmd, net), f"run `{cmd}`" + (" with network" if net else ""))):
            return r
        timeout = min(int(a.get("timeout_s") or 300), 1800)
        return self._run(cmd, self.cwd, net, timeout, "bash")

    def check(self, a: dict) -> ToolResult:
        name = a["name"]
        spec = self.checks.get(name)
        if spec is None:
            return ToolResult(False, f"no check named {name!r}")
        net = bool(spec.get("net"))
        if (r := self._gate(self.policy.check_exec(spec["run"], net), f"check {name}" + (" with network" if net else ""))):
            return r
        cwd = self.root / spec.get("cwd", ".")
        return self._run(spec["run"], cwd, net, float(spec.get("timeout", 900)), f"check:{name}")
