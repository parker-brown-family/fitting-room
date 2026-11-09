"""h0 host: a non-interactive evaluation host.

Implements the host contract in tcballard/build-omarchy-themes (evals/harness/README.md):
one JSON request on stdin; JSONL on stdout whose first line is `host_metadata` (emitted
before any model call) and whose last line is `host_summary` with `actions_complete`;
complete tool-call arguments; tool results as typed segments, `skill_file` for anything
read from the skill root and `task_output` otherwise; HOME set to the fixture's `home/`
when there is one; host configuration kept under /tmp.

Request fields used: request, fixture_path, skill_path, model, effort, max_turns,
max_budget_usd, time_limit_seconds. Unknown fields are ignored and echoed in metadata.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from h0 import __version__, config
from h0.cli import build_agent
from h0.trace import Trace


def _skill_index(skill_root: Path) -> tuple[str, list[str]]:
    lines, names = [], []
    for sk in sorted(skill_root.glob("*/SKILL.md")):
        text = sk.read_text(errors="replace")
        m = re.search(r"^description:\s*(.+?)(?:\n[a-z_]+:|\n---)", text, re.S | re.M)
        desc = " ".join(m.group(1).split())[:300] if m else ""
        names.append(sk.parent.name)
        lines.append(f"- {sk.parent.name}: {desc} (read {sk})")
    if not lines:
        return "", names
    return ("## Skills\nThese skills are installed. When one matches the request, read its SKILL.md with the read "
            "tool before acting, then any file it points to.\n" + "\n".join(lines)), names


class HostUI:
    def text(self, s): pass
    def thinking(self, s): pass
    def tool_start(self, n, a): pass
    def tool_end(self, n, r, ms): pass
    def note(self, s): pass
    def turn_end(self, t): pass
    def ask(self, w, d): return False


def cmd_host(args) -> int:
    req = json.loads(sys.stdin.read())
    fixture = Path(req["fixture_path"]).resolve()
    skill_root = Path(req["skill_path"]).resolve() if req.get("skill_path") else None
    s = config.Settings(model=req.get("model") or config.DEFAULT_MODEL, effort=req.get("effort"), headless=True, mode="yolo")
    s.max_turns = int(req.get("max_turns") or s.max_turns)
    s.budget_usd = req.get("max_budget_usd")
    out = sys.stdout

    def echo(rec: dict) -> None:
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()

    tdir = Path(os.environ.get("TMPDIR", "/tmp")) / "h0-host"
    trace = Trace(tdir / f"host-{int(time.time())}.jsonl", echo=echo)
    index, names = _skill_index(skill_root) if skill_root and skill_root.is_dir() else ("", [])
    from h0.fitting import Fitting
    fit = Fitting(name="host", root=fixture, map_md=index) if index else None
    if fit and skill_root:
        fit.policy = {"read_roots": [str(skill_root)], "ro_paths": [str(skill_root)]}
    agent = build_agent(s, fixture, fit, HostUI(), trace)
    if (fixture / "home").is_dir():
        agent.toolbox.sandbox.env["HOME"] = str(fixture / "home")
    for k, v in {"XDG_CONFIG_HOME": "/tmp/config", "XDG_CACHE_HOME": "/tmp/cache", "XDG_DATA_HOME": "/tmp/data",
                 "XDG_STATE_HOME": "/tmp/state", "CARGO_HOME": "/tmp/cargo-home", "CARGO_TARGET_DIR": "/tmp/cargo-target"}.items():
        agent.toolbox.sandbox.env.setdefault(k, v)
    tools = [x.name for x in agent.toolbox.specs]
    trace.emit("host_metadata", configured_model_id=s.model, requested_effort=s.effort, host_version=f"harness-0 {__version__}",
               tools=tools, loaded_skills=names, requested_tools=req.get("tools"), fixture=str(fixture))
    # Type every tool result as a contract segment: a skill file or task output.
    real_call = agent.toolbox.call

    def call(name, a):
        res = real_call(name, a)
        path = a.get("path") if isinstance(a, dict) else None
        kind = "task_output"
        if name == "read" and path and skill_root:
            p = Path(os.path.realpath(path if os.path.isabs(path) else fixture / path))
            if str(p).startswith(str(skill_root)):
                kind = "skill_file"
                res.meta["segments"] = [{"kind": "skill_file", "path": str(p), "text": res.text}]
        res.meta.setdefault("segments", [{"kind": kind, "text": res.text}])
        return res

    agent.toolbox.call = call
    started = time.monotonic()
    summary = agent.run(req["request"])
    requests = [{"query_source": "main", "model": s.model, "effort": s.effort}] * summary["totals"]["turns"]
    trace.emit("final", text=summary["final"], stop=summary["stop"])
    done = summary["stop"] == "end_turn"
    trace.emit("host_summary", actions_complete=done, exit_status=0 if done else 1, stop=summary["stop"],
               totals=summary["totals"], requests=requests, elapsed_seconds=round(time.monotonic() - started, 1))
    trace.close()
    return 0 if done else 1


def register(sp) -> None:
    h = sp.add_parser("host", help="evaluation host: one JSON request on stdin, a JSONL trace on stdout")
    h.set_defaults(func=cmd_host)
