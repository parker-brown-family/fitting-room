"""Fittings: the data that tailors h0 to one project.

A fitting is a directory:
  fitting.toml   name, root, model defaults, tools, checks, triggers, policy, budgets
  map.md         the project map injected into the system prompt (keep it short)
  lessons.jsonl  {id, text, helpful, harmful, source} — injected as "may not apply"
  sources.json   what the fitting was built from, with hashes, for drift checks
  tasks.jsonl    the project's own eval tasks (optional)

Resolution mirrors a project resolver: the fitting whose root is the longest prefix
of the working directory wins.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from h0.config import DATA, REPO

MAP_LINE_CAP = 160
LESSON_CAP = 12


@dataclass
class Fitting:
    name: str
    root: Path
    dir: Path | None = None
    about: str = ""
    model: str | None = None
    effort: str | None = None
    tools: list[str] | None = None
    checks: dict = field(default_factory=dict)
    triggers: list[dict] = field(default_factory=list)
    policy: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    verify: str | None = None
    map_md: str = ""
    lessons: list[dict] = field(default_factory=list)
    version: int = 1

    def prompt_layer(self) -> str:
        out = [f"# Project: {self.name}"]
        if self.map_md.strip():
            lines = self.map_md.strip().splitlines()
            out.append("\n".join(lines[:MAP_LINE_CAP]))
        if self.checks:
            out.append("## Checks\nRun these with the check tool rather than guessing commands:\n" + "\n".join(
                f"- {n}: {c.get('about') or c['run']}" for n, c in sorted(self.checks.items())))
        if self.verify:
            out.append(f"Before you finish, the harness runs the `{self.verify}` check; a failure comes back to you.")
        live = [l for l in self.lessons if l.get("harmful", 0) <= l.get("helpful", 0)]
        live.sort(key=lambda l: (l.get("helpful", 0) - l.get("harmful", 0)), reverse=True)
        if live:
            out.append("## Lessons from earlier sessions (each may not apply; check it fits before relying on it)\n" + "\n".join(
                f"- [{l['id']}] {l['text']}" for l in live[:LESSON_CAP]))
        return "\n\n".join(out)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, Path):
        v = str(v)
    s = str(v)
    if "\n" in s:
        return '"""' + s.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
    return json.dumps(s, ensure_ascii=False)


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def dump_toml(data: dict) -> str:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and not (isinstance(v, list) and v and isinstance(v[0], dict))}
    for k, v in scalars.items():
        if v is not None:
            lines.append(f"{_key(k)} = {_toml_value(v)}")
    for k, v in data.items():
        if isinstance(v, dict):
            subs = {sk: sv for sk, sv in v.items() if isinstance(sv, dict)}
            flat = {sk: sv for sk, sv in v.items() if not isinstance(sv, dict) and sv is not None}
            if flat or not subs:
                lines.append(f"\n[{_key(k)}]")
                lines += [f"{_key(sk)} = {_toml_value(sv)}" for sk, sv in flat.items()]
            for sk, sv in subs.items():
                lines.append(f"\n[{_key(k)}.{_key(sk)}]")
                lines += [f"{_key(kk)} = {_toml_value(vv)}" for kk, vv in sv.items() if vv is not None]
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for item in v:
                lines.append(f"\n[[{_key(k)}]]")
                lines += [f"{_key(kk)} = {_toml_value(vv)}" for kk, vv in item.items() if vv is not None]
    return "\n".join(lines).lstrip() + "\n"


def load(d: Path) -> Fitting:
    d = Path(d)
    data = tomllib.loads((d / "fitting.toml").read_text())
    lessons = []
    lf = d / "lessons.jsonl"
    if lf.exists():
        lessons = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()]
    mp = d / "map.md"
    root = Path(os.path.expanduser(data["root"]))
    return Fitting(
        name=data["name"], root=root, dir=d, about=data.get("about", ""), model=data.get("model"),
        effort=data.get("effort"), tools=data.get("tools"), checks=data.get("checks", {}),
        triggers=data.get("triggers", []), policy=data.get("policy", {}), budgets=data.get("budgets", {}),
        verify=data.get("verify"), map_md=mp.read_text() if mp.exists() else "", lessons=lessons,
        version=data.get("version", 1),
    )


def save(f: Fitting, d: Path) -> Path:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": f.name, "version": f.version, "root": str(f.root), "about": f.about, "model": f.model,
            "effort": f.effort, "tools": f.tools, "verify": f.verify, "budgets": f.budgets or None,
            "policy": f.policy or None, "checks": f.checks or None, "triggers": f.triggers or None}
    data = {k: v for k, v in data.items() if v not in (None, "", [], {})}
    (d / "fitting.toml").write_text(dump_toml(data))
    (d / "map.md").write_text(f.map_md.rstrip() + "\n")
    with open(d / "lessons.jsonl", "w") as out:
        for l in f.lessons:
            out.write(json.dumps(l, ensure_ascii=False) + "\n")
    f.dir = d
    return d


def search_paths() -> list[Path]:
    paths = [Path(p) for p in os.environ.get("H0_FITTINGS_PATH", "").split(":") if p]
    return paths + [DATA / "fittings", REPO / "fittings"]


def all_fittings() -> list[Fitting]:
    seen, out = set(), []
    for base in search_paths():
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if (d / "fitting.toml").exists() and d.name not in seen:
                try:
                    out.append(load(d))
                    seen.add(d.name)
                except (OSError, KeyError, tomllib.TOMLDecodeError):
                    continue
    return out


def resolve(cwd: Path) -> Fitting | None:
    cwd = Path(os.path.realpath(cwd))
    best, best_len = None, -1
    for f in all_fittings():
        r = Path(os.path.realpath(f.root))
        if (cwd == r or str(cwd).startswith(str(r) + os.sep)) and len(str(r)) > best_len:
            best, best_len = f, len(str(r))
    return best


def find(spec: str) -> Fitting:
    p = Path(os.path.expanduser(spec))
    if (p / "fitting.toml").exists():
        return load(p)
    for f in all_fittings():
        if f.name == spec:
            return f
    raise FileNotFoundError(f"no fitting named or at {spec!r} (searched {', '.join(map(str, search_paths()))})")


class Triggers:
    """Rules bound to moments: before or after a tool call, matched on its command or path.

    action inject  — append a note to the tool result (the moment the rule applies)
    action require — run a named check first; if it fails, the call is refused with the output
    action block   — refuse the call with the note as the reason
    """

    def __init__(self, triggers: list[dict], run_check=None, emit=None):
        self.rules = []
        for t in triggers or []:
            when, _, tool = t.get("on", "").partition(":")
            self.rules.append({**t, "_when": when, "_tool": tool or "*", "_rx": re.compile(t.get("match", ".*")), "_fired": 0})
        self.run_check = run_check
        self.emit = emit or (lambda *a, **k: None)

    @staticmethod
    def subject(tool: str, args: dict) -> str:
        return str(args.get("command") or args.get("path") or args.get("name") or json.dumps(args))

    def _matching(self, when: str, tool: str, args: dict):
        s = self.subject(tool, args)
        for r in self.rules:
            if r["_when"] == when and r["_tool"] in ("*", tool) and r["_rx"].search(s):
                yield r

    def before(self, tool: str, args: dict):
        notes: list[str] = []
        for r in self._matching("before", tool, args):
            action = r.get("action", "inject")
            if action == "block":
                self.emit("trigger", rule=r.get("id") or r.get("match"), action="block", tool=tool)
                return r.get("note") or "blocked by a project rule", notes
            if action == "require" and self.run_check and r.get("check"):
                ok, out = self.run_check(r["check"])
                self.emit("trigger", rule=r.get("id") or r.get("match"), action="require", check=r["check"], ok=ok, tool=tool)
                if not ok:
                    return f"{r.get('note') or 'a required check failed'}\n`{r['check']}` output:\n{out}", notes
            if action == "inject" and (not r.get("once", True) or r["_fired"] == 0):
                r["_fired"] += 1
                notes.append(r.get("note", ""))
                self.emit("trigger", rule=r.get("id") or r.get("match"), action="inject", tool=tool)
        return None, notes

    def after(self, tool: str, args: dict) -> list[str]:
        notes = []
        for r in self._matching("after", tool, args):
            if r.get("action") == "require" and self.run_check and r.get("check"):
                ok, out = self.run_check(r["check"])
                self.emit("trigger", rule=r.get("id") or r.get("match"), action="require", check=r["check"], ok=ok, tool=tool, when="after")
                if not ok:
                    notes.append(f"{r.get('note') or 'a required check failed'} The `{r['check']}` check failed after this change:\n{out[-1500:]}")
                continue
            if not r.get("once", True) or r["_fired"] == 0:
                r["_fired"] += 1
                notes.append(r.get("note", ""))
                self.emit("trigger", rule=r.get("id") or r.get("match"), action="inject", tool=tool, when="after")
        return notes
