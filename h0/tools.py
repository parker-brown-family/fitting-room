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

HOME_REAL = Path(os.path.realpath(Path.home()))

SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".next", ".venv", "venv", "__pycache__",
             ".cache", ".turbo", ".svelte-kit", "coverage", ".pytest_cache", ".mypy_cache", ".gradle"}
NET_FAIL = re.compile(r"Could not resolve|Network is unreachable|Temporary failure in name resolution|"
                      r"Name or service not known|getaddrinfo|ENOTFOUND|EAI_AGAIN|ENETUNREACH|ECONNREFUSED|"
                      r"Failed to connect|Could not connect|Connection refused|connect: |unable to access", re.I)


READERS = {"cat", "head", "tail", "sed", "nl", "less", "more", "bat"}   # commands that print a file


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


JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}


def bad_args(name: str, schema: dict, args) -> str | None:
    """Why these arguments cannot be run, in words the model can act on, or None. A call cut off
    by the output limit arrives as unparsable JSON; without this it surfaced as `KeyError: 'path'`."""
    if not isinstance(args, dict):
        return f"{name} takes an object of arguments, not {type(args).__name__}"
    if "_unparsed" in args:
        return (f"your {name} call was cut off before its arguments were complete (output limit), so it did not run. "
                "Send it again; if it carried a large file, write it in smaller parts or use edit.")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return f"{name} is missing required argument(s): {', '.join(missing)}"
    for k, v in args.items():
        spec = schema.get("properties", {}).get(k)
        if spec is None:
            continue
        want = JSON_TYPES.get(spec.get("type"))
        if want and (not isinstance(v, want) or (spec.get("type") != "boolean" and isinstance(v, bool))):
            if spec.get("type") == "integer" and isinstance(v, str) and v.strip().isdigit():
                args[k] = int(v)  # small models often quote numbers; the meaning is unambiguous
                continue
            return f"{name}: argument {k!r} should be {spec['type']}, got {type(v).__name__}"
    return None


def _norm(line: str) -> str:
    return line.rstrip()


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
        self.checkpoints: list[dict[Path, bytes | None]] = []   # per request: each file's content before its first change
        self.local = local
        self.tree = None    # an h0.undo.TreeUndo when the fitting declares undo = "tree"
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
            ToolSpec("bash", "Run a bash command in the project sandbox: project writable, home hidden, no network. Every call already starts in the project root, so there is no need to cd there; cd into a subdirectory within the same command when you need one.",
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
        if (why := bad_args(name, spec.schema, args)):
            return ToolResult(False, why, {"bad_args": True})
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
    def _gate(self, decide: Callable[[], Decision], what: str) -> ToolResult | None:
        """Ask the policy, put each `ask` to the person, and ask the policy again after every yes.
        One command can hit several gates in order (destructive, then network, then the rule of
        two); approving the first must not let the call skip the rest."""
        once: list[str] = []
        try:
            for _ in range(4):
                d = decide()
                if d.verdict == "allow":
                    return None
                if d.verdict != "ask" or not self.ask or d.key in self.policy.approvals:
                    break
                answer = self.ask(what, d)
                if not answer:
                    break
                if answer != "always":
                    once.append(d.key)
                self.policy.approve(d.key)
        finally:
            for k in once:
                self.policy.approvals.discard(k)
        self.emit("policy_denied", what=what, rule=d.rule, reason=d.reason)
        return ToolResult(False, f"refused by policy ({d.rule}): {d.reason}", {"denied": d.rule})

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    def _path(self, asked: str, must_exist: bool = True) -> tuple[Path, str]:
        """Resolve a path the model gave, and in a session whose home was moved into the project,
        read the two unambiguous slips by their only meaning: `/.config/x` (a dot-directory at the
        filesystem root, which never exists) and the person's real home path. A 9B model repeated
        both after being shown the right path, five times in one trial. The result is checked by the
        policy like any path; the returned note tells the model what was read."""
        p = self.policy.resolve(asked, self.cwd)
        home = self.policy.home
        if home is None or Path(os.path.realpath(home)) == HOME_REAL:
            return p, ""
        alt = None
        parts = Path(asked).parts
        if asked.startswith("/") and len(parts) > 1 and parts[1].startswith(".") and parts[1] not in (".", "..") \
                and not p.exists() and not (not must_exist and p.parent.exists()):
            alt = self.policy.resolve(str(home) + asked, self.cwd)
        elif self.policy.check_read(p).rule == "home-outside-project":
            # the real home is hidden from this session, so a path into it can only mean the session's home
            alt = self.policy.resolve(str(home) + str(p)[len(str(HOME_REAL)):], self.cwd)
        if alt is not None and (alt.exists() or (not must_exist and alt.parent.exists())):
            return alt, f"[harness] {asked} was read as {self._rel(alt)}: in this session ~ is the project root."
        return p, ""

    def _missing(self, p: Path, asked: str) -> str:
        """`no such file`, with the likeliest intended paths. In 1,210 eval traces the common misses
        were a root-relative path written absolute (`/.config/hypr/x.lua`) and a right file name
        in the wrong directory; both cost a turn of listing directories to recover."""
        found: list[str] = []
        stripped = asked.lstrip("/")
        if asked.startswith("/") and stripped and (self.root / stripped).exists() and self._readable_quiet(self.root / stripped):
            found.append(stripped)
        if not found:
            n = 0
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".h0-")]
                n += len(filenames)
                if p.name in filenames and self._readable_quiet(Path(dirpath) / p.name):
                    found.append(self._rel(Path(dirpath) / p.name))
                if len(found) >= 5 or n > 20_000:
                    break
        hint = f" Did you mean: {', '.join(sorted(found)[:5])}?" if found else ""
        return f"no such file: {self._rel(p)}.{hint}"

    # -- handlers ---------------------------------------------------------------
    def read(self, a: dict) -> ToolResult:
        p, note = self._path(a["path"])
        r = self._read(p, a)
        if note and r.ok:
            r.text = note + "\n" + r.text
        return r

    def _read(self, p: Path, a: dict) -> ToolResult:
        if (r := self._gate(lambda: self.policy.check_read(p), f"read {p}")):
            return r
        if p.is_dir():
            entries = sorted(os.listdir(p))
            shown = [e + ("/" if (p / e).is_dir() else "") for e in entries[:300]]
            more = f"\n… {len(entries) - 300} more" if len(entries) > 300 else ""
            return ToolResult(True, "\n".join(shown) + more)
        if not p.exists():
            return ToolResult(False, self._missing(p, a["path"]))
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

    def checkpoint(self) -> None:
        self.checkpoints = self.checkpoints[-19:] + [{}]
        if self.tree:
            self.tree.checkpoint()

    def undo(self) -> list[str]:
        """Put back every file the most recent request that changed anything changed. With tree undo
        that is every file under the writable roots, whatever wrote it, and a file that could not be
        put back is named with the reason; otherwise only the file tools' writes are undone."""
        if self.tree:
            r = self.tree.undo()
            if not r:
                return []
            out = [self._rel(Path(p)) for p in r["restored"]]
            out += [f"{self._rel(Path(p))} (removed: the request created it)" for p in r["removed"]]
            out += [f"{self._rel(Path(p))} (NOT put back)" for p in r["failed"]]
            if r["partial"]:
                out.append("(more files than undo indexes; files past the limit were not checked)")
            return out
        while self.checkpoints:
            cp = self.checkpoints.pop()
            if not cp:
                continue
            for p, before in cp.items():
                if before is None:
                    p.unlink(missing_ok=True)
                else:
                    self._write_atomic(p, before.decode("utf-8", errors="surrogateescape"), record=False)
            return sorted(self._rel(p) for p in cp)
        return []

    def _write_atomic(self, p: Path, content: str, record: bool = True) -> None:
        if record and self.checkpoints and p not in self.checkpoints[-1]:
            self.checkpoints[-1][p] = p.read_bytes() if p.exists() else None
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = p.stat().st_mode & 0o7777 if p.exists() else 0o644
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".h0-")
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, p)

    def write(self, a: dict) -> ToolResult:
        p, note = self._path(a["path"], must_exist=False)
        r = self._write(p, a)
        if note and r.ok:
            r.text = note + "\n" + r.text
        return r

    def _write(self, p: Path, a: dict) -> ToolResult:
        if (r := self._gate(lambda: self.policy.check_write(p), f"write {p}")):
            return r
        if p.exists() and p not in self.seen:
            return ToolResult(False, f"{self._rel(p)} exists; read it before replacing it, or use edit")
        self._write_atomic(p, a["content"])
        self.seen.add(p)
        self.emit("file_write", path=self._rel(p), bytes=len(a["content"].encode()))
        return ToolResult(True, f"wrote {len(a['content'].encode())} bytes to {self._rel(p)}")

    def edit(self, a: dict) -> ToolResult:
        p, note = self._path(a["path"])
        r = self._edit(p, a)
        if note and r.ok:
            r.text = note + "\n" + r.text
        return r

    def _edit(self, p: Path, a: dict) -> ToolResult:
        if (r := self._gate(lambda: self.policy.check_write(p), f"edit {p}")):
            return r
        if not p.exists():
            return ToolResult(False, self._missing(p, a["path"]) + " To create a new file, use write.")
        text = p.read_text(encoding="utf-8", errors="replace")
        old, new = a["old"], a["new"]
        if old == new:
            return ToolResult(False, "old and new are identical")
        n = text.count(old)
        if n == 0:
            span = self._loose_span(text, old)
            if span is None:
                return ToolResult(False, f"old text not found in {self._rel(p)}.{self._nearest(text, old)}")
            # the same lines apart from trailing whitespace, found once: apply it rather than
            # spend a turn re-reading (the rest of each line, indentation included, must match)
            updated = text[:span[0]] + new + text[span[1]:]
            self._write_atomic(p, updated)
            self.seen.add(p)
            line = text.count("\n", 0, span[0]) + 1
            self.emit("file_edit", path=self._rel(p), replacements=1, line=line, loose=True)
            return ToolResult(True, f"edited {self._rel(p)} at line {line} (matched ignoring trailing whitespace)")
        if n > 1 and not a.get("replace_all"):
            return ToolResult(False, f"old text appears {n} times in {self._rel(p)}; add surrounding text or set replace_all")
        updated = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
        self._write_atomic(p, updated)
        self.seen.add(p)
        at = updated.find(new)
        line = updated.count("\n", 0, at) + 1 if at >= 0 else None
        self.emit("file_edit", path=self._rel(p), replacements=n if a.get("replace_all") else 1, line=line)
        return ToolResult(True, f"edited {self._rel(p)}" + (f" at line {line}" if line else "") + (f" ({n} replacements)" if a.get("replace_all") else ""))

    @staticmethod
    def _loose_span(text: str, old: str) -> tuple[int, int] | None:
        """Character span of the one place `old` matches line by line once trailing whitespace is
        ignored, or None when there is no such place or more than one."""
        nl = old.endswith("\n")
        want = [_norm(l) for l in (old[:-1] if nl else old).split("\n")]
        if not any(w.strip() for w in want):
            return None
        lines = text.split("\n")
        starts, pos = [], 0
        for l in lines:
            starts.append(pos)
            pos += len(l) + 1
        hits = [i for i in range(len(lines) - len(want) + 1)
                if all(_norm(lines[i + j]) == w for j, w in enumerate(want))]
        if len(hits) != 1:
            return None
        i = hits[0]
        end = starts[i + len(want) - 1] + len(lines[i + len(want) - 1])
        if nl and end < len(text):
            end += 1
        return starts[i], end

    @staticmethod
    def _nearest(text: str, old: str) -> str:
        """Show the region of the file most like `old`, numbered as read numbers lines, so the next
        try can copy it exactly instead of reading the file again."""
        import difflib
        want = old.strip("\n").split("\n")
        if re.match(r"^\s*\d+\t", want[0]) and all(re.match(r"^\s*\d+\t", w) for w in want if w.strip()):
            return " Your old text starts each line with a line number and a tab, as read shows them; leave those out."
        lines = text.split("\n")
        k = len(want)
        if not lines or k > 200:
            return " Read the region again and copy it exactly."
        target = "\n".join(w.strip() for w in want)
        best, best_i = 0.0, 0
        for i in range(max(1, len(lines) - k + 1)):
            window = "\n".join(l.strip() for l in lines[i:i + k])
            sm = difflib.SequenceMatcher(None, target, window, autojunk=False)
            if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
                continue
            r = sm.ratio()
            if r > best:
                best, best_i = r, i
        if best < 0.5:
            return " Nothing in the file is close to it; read the file again."
        shown = "\n".join(f"{j}\t{l[:300]}" for j, l in enumerate(lines[best_i:best_i + min(k, 40)], best_i + 1))
        return f" The closest text (lines {best_i + 1}-{best_i + min(k, 40)}, {best:.0%} similar) is:\n{shown}"

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

    def _readable_quiet(self, f: Path) -> bool:
        return any(True for _ in self._readable(f))

    def _readable(self, f: Path):
        real = Path(os.path.realpath(f))
        if self.policy.check_read(real).allowed and not redact.is_secret_path(str(real)):
            yield f

    def grep(self, a: dict) -> ToolResult:
        base = self.policy.resolve(a.get("path") or ".", self.cwd)
        if (r := self._gate(lambda: self.policy.check_read(base), f"grep {base}")):
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
        if (r := self._gate(lambda: self.policy.check_read(base), f"glob {base}")):
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
        if (r := self._gate(lambda: self.policy.check_exec(cmd, net), f"run `{cmd}`" + (" with network" if net else ""))):
            return r
        timeout = min(int(a.get("timeout_s") or 300), 1800)
        res = self._run(cmd, self.cwd, net, timeout, "bash")
        if res.ok:
            self.seen |= self._shown(cmd)
        return res

    def _shown(self, cmd: str) -> set[Path]:
        """Files a command printed whole or in part (`cat f`, `head f`, `sed -n 1,80p f`). A model
        that has looked at a file through bash has read it; refusing its next write only sent it
        back to bash to write the file with a heredoc, which no rule checks."""
        import shlex
        try:
            words = shlex.split(cmd, comments=True)
        except ValueError:
            return set()
        if not READERS & {os.path.basename(w) for w in words}:
            return set()
        out = set()
        for w in words:
            if w.startswith("-") or not w or any(c in w for c in "*?|<>;&"):
                continue
            p = self.policy.resolve(w, self.cwd)
            if p.is_file() and self.policy.check_read(p).allowed:
                out.add(p)
        return out

    def check(self, a: dict) -> ToolResult:
        name = a["name"]
        spec = self.checks.get(name)
        if spec is None:
            return ToolResult(False, f"no check named {name!r}")
        net = bool(spec.get("net"))
        if (r := self._gate(lambda: self.policy.check_exec(spec["run"], net), f"check {name}" + (" with network" if net else ""))):
            return r
        cwd = self.root / spec.get("cwd", ".")
        return self._run(spec["run"], cwd, net, float(spec.get("timeout", 900)), f"check:{name}")
