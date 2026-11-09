"""Eval tasks, disposable workspaces, and the trial runner.

A task is one JSON object:
  id, project, kind ("authored" | "mined" | "security"), prompt,
  setup:  {"type": "git", "repo": path, "commit": rev|null, "revert": [paths from parent], "links": ["node_modules"]}
        | {"type": "copy", "from": dir}
  checks: [{"run": cmd, "cwd": ".", "expect": 0, "timeout": s}]   all must pass
  attack: {"attempt": regex over tool calls, "achieved": cmd (exit 0 = achieved)}   security tasks only
  budget: {"max_turns": n, "usd": x}
A trial's `passed` is True, False, or None when it could not be scored (setup or provider
failure); None is reported as unscored, never folded into a pass rate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from h0 import config, fitting as fit_mod
from h0.sandbox import Sandbox

WS_BASE = Path(os.environ.get("H0_WS", Path.home() / ".cache/h0/ws"))
# Every eval command, in every arm, runs under the same ceiling, so a parallel sweep
# cannot push a shared desktop into the OOM killer and the arms stay comparable.
EVAL_RESOURCES = {"memory_max": os.environ.get("H0_EVAL_MEMORY_MAX", "4G"), "cpu_weight": 30, "nice": 15}
_lock = threading.Lock()


def make_workspace(src: Path, commit: str | None = None, revert: list[str] | None = None,
                   links: list[str] | None = None, tag: str = "ws") -> Path:
    src = Path(os.path.realpath(os.path.expanduser(src)))
    WS_BASE.mkdir(parents=True, exist_ok=True)
    ws = WS_BASE / f"{tag}-{pysecrets.token_hex(4)}"
    if (src / ".git").exists():
        # No history in the workspace: the tree at `commit`, with `revert` put back to its parent,
        # committed once. A clone carried the task's own solution commit, and 26 of one mined suite's 168
        # trials read it with `git show` (2026-11-09). The answer must be structurally out of reach.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        rev = commit or "HEAD"
        ws.mkdir()
        arc = subprocess.run(["git", "-C", str(src), "archive", "--format=tar", rev], check=True, capture_output=True, env=env)
        subprocess.run(["tar", "-x", "-C", str(ws)], input=arc.stdout, check=True, capture_output=True)
        for p in revert or []:
            old = subprocess.run(["git", "-C", str(src), "show", f"{rev}~1:{p}"], capture_output=True, env=env)
            target = ws / p
            if old.returncode == 0:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(old.stdout)
            else:  # the commit added this file: the task starts without it
                target.unlink(missing_ok=True)
        subprocess.run(["git", "init", "-q", str(ws)], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "-c", "user.email=h0@local", "-c", "user.name=h0", "commit", "-qm",
                        "h0: task baseline", "--allow-empty"], capture_output=True, env=env)
    else:
        shutil.copytree(src, ws, symlinks=True)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "init", "-q", str(ws)], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "-c", "user.email=h0@local", "-c", "user.name=h0", "commit", "-qm",
                        "h0: task baseline"], check=True, env=env, capture_output=True)
    for l in links or []:
        s = src / l
        if s.exists() and not (ws / l).exists():
            subprocess.run(["cp", "-al", str(s), str(ws / l)], check=False, capture_output=True)
    return ws


def remove_workspace(ws: Path) -> None:
    if str(ws).startswith(str(WS_BASE)):
        shutil.rmtree(ws, ignore_errors=True)


def load_tasks(path: Path) -> list[dict]:
    tasks = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            tasks.append(json.loads(line))
    return tasks


@dataclass
class Trial:
    task: str
    project: str
    kind: str
    arm: str
    model: str
    trial: int
    passed: bool | None = None
    check_results: list = field(default_factory=list)
    stop: str | None = None
    error: str | None = None
    totals: dict = field(default_factory=dict)
    wall_s: float | None = None
    attempted_attack: bool | None = None
    achieved_attack: bool | None = None
    flags: dict = field(default_factory=dict)
    diff_stat: str | None = None
    trace: str | None = None
    final: str | None = None


def _fitting_for(arm: dict, ws: Path):
    if not arm.get("fitting"):
        return None
    f = fit_mod.find(arm["fitting"])
    old = str(f.root)
    f.root = ws
    pol = json.loads(json.dumps(f.policy or {}))
    for k in ("read_roots", "write_roots", "ro_paths", "rw_paths"):
        pol[k] = [p.replace(old, str(ws)) for p in pol.get(k, [])]
    f.policy = pol
    if arm.get("no_lessons"):
        f.lessons = []
    if arm.get("no_verify"):
        f.verify = None
    return f


class QuietUI:
    def text(self, s): pass
    def thinking(self, s): pass
    def tool_start(self, n, a): pass
    def tool_end(self, n, r, ms): pass
    def note(self, s): pass
    def turn_end(self, t): pass
    def ask(self, w, d): return False


def apply_task_sandbox(sb, spec: dict | None, policy=None) -> None:
    """A task's build environment (shared caches, re-anchored path dependencies) belongs to
    the task, not to a fitting: every arm needs it to build the project at all."""
    if not spec:
        return
    sb.rw_paths += [Path(os.path.expanduser(p)) for p in spec.get("rw_paths", [])]
    sb.ro_paths += [Path(os.path.expanduser(p)) for p in spec.get("ro_paths", [])]
    sb.binds += [list(b) for b in spec.get("binds", [])]
    sb.env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    if spec.get("resources"):
        sb.resources = dict(spec["resources"])
    if policy is not None:
        policy.read_roots += [Path(os.path.expanduser(p)) for p in spec.get("read_roots", [])]


LOCK_DIR = Path(os.environ.get("H0_LOCK_DIR", Path.home() / ".cache/h0/locks"))


def shared_caches(task: dict) -> list[str]:
    """The writable build caches a task's sandbox shares across workspaces, sorted."""
    return sorted({str(Path(os.path.expanduser(p))) for p in ((task.get("sandbox") or {}).get("rw_paths") or [])})


class exclusive:
    """Hold every shared build cache a task writes for the length of a trial.

    Two workspaces building into one cargo target directory hand each other their binaries:
    cargo names artifacts by the package's path relative to its workspace, so the second
    build overwrites the first's, and a test run can execute a program built from the other
    workspace's tree, or one whose baked-in `CARGO_MANIFEST_DIR` the sandbox hides. That
    decided two of td-v1's eight trials (2026-11-09). The lock is an flock on a file named
    for the cache under LOCK_DIR, so it holds across threads and across h0 processes (an eval
    beside a mine or a re-score). Trials that share no cache still run in parallel; locks are
    taken in sorted order, so two tasks cannot deadlock."""

    def __init__(self, task: dict):
        self.paths = [LOCK_DIR / (hashlib.sha256(k.encode()).hexdigest()[:16] + ".lock") for k in shared_caches(task)]
        self._fds: list[int] = []

    def __enter__(self):
        import fcntl
        if self.paths:
            LOCK_DIR.mkdir(parents=True, exist_ok=True)
        for path in self.paths:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._fds.append(fd)
        return self

    def __exit__(self, *exc):
        import fcntl
        for fd in reversed(self._fds):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._fds = []
        return False


def run_checks(ws: Path, checks: list[dict], f=None, final: str | None = None, home: bool = False, task_sandbox: dict | None = None) -> list[dict]:
    pol = (f.policy if f else {}) or {}
    env = {k: str(v) for k, v in (pol.get("env") or {}).items()}
    if home:
        env["HOME"] = str(ws)
    sb = Sandbox(root=ws, rw_paths=[Path(os.path.expanduser(p)) for p in pol.get("rw_paths", [])],
                 ro_paths=[Path(os.path.expanduser(p)) for p in pol.get("ro_paths", [])],
                 env=env, resources=pol.get("resources") or dict(EVAL_RESOURCES), binds=pol.get("binds") or [])
    apply_task_sandbox(sb, task_sandbox)
    out = []
    for c in checks:
        if "final_regex" in c:
            ok = bool(final) and re.search(c["final_regex"], final, re.I) is not None
            out.append({"final_regex": c["final_regex"], "ok": ok})
            continue
        r = sb.run(c["run"], cwd=ws / c.get("cwd", "."), timeout=float(c.get("timeout", 600)))
        ok = (r.exit == c.get("expect", 0)) and not r.timed_out
        out.append({"run": c["run"], "ok": ok, "exit": r.exit, "timed_out": r.timed_out, "tail": r.output[-800:]})
    return out


def run_trial(task: dict, arm: dict, model: str, trial_no: int, out_dir: Path, effort: str | None = None,
              keep: bool = False) -> Trial:
    from h0.cli import build_agent, host_metadata
    from h0.trace import Trace
    t = Trial(task=task["id"], project=task.get("project", ""), kind=task.get("kind", "authored"), arm=arm["name"],
              model=model, trial=trial_no)
    ws = None
    started = time.monotonic()
    try:
        setup = task["setup"]
        if setup["type"] == "git":
            ws = make_workspace(Path(setup["repo"]), setup.get("commit"), setup.get("revert"), setup.get("links"), tag=task["id"])
        elif setup["type"] == "copy":
            ws = make_workspace(Path(setup["from"]), tag=task["id"])
        else:
            raise ValueError(f"unknown setup type {setup['type']}")
        f = _fitting_for(arm, ws)
        s = config.Settings(model=model, headless=True, mode="auto")
        if effort:
            s.effort = effort
        if arm.get("provider"):
            s.provider = arm["provider"]
            s.base_url = arm.get("base_url")
        if s.provider == "openai" or arm.get("local"):
            s.apply_local_profile()
        b = task.get("budget", {})
        s.max_turns = int(b.get("max_turns", 40))
        s.budget_usd = b.get("usd", 2.0)
        tpath = out_dir / "traces" / f"{task['id']}--{arm['name']}--{model}--{trial_no}.jsonl"
        tr = Trace(tpath)
        agent = build_agent(s, ws, f, QuietUI(), tr)
        if os.environ.get("H0_EVAL_JUDGE") != "jev":
            # evals measure the fitting; the judge is measured on its own (evals/judge), and is
            # held off in both arms so runs stay comparable with those before it was on by default
            agent.policy.judge = None
        if setup.get("home_is_root"):  # tcballard host contract: HOME is the fixture's home when it has one
            agent.toolbox.sandbox.env["HOME"] = str(ws)
        apply_task_sandbox(agent.toolbox.sandbox, task.get("sandbox"), agent.policy)
        if not agent.toolbox.sandbox.resources:
            agent.toolbox.sandbox.resources = dict(EVAL_RESOURCES)
        if f is not None and f.verify and "check" in agent.toolbox.by_name:
            # A verify gate that already fails on the untouched task would trap the agent in
            # breakage it did not cause (mined tasks start at old commits): test it first.
            pre = agent.toolbox.call("check", {"name": f.verify})
            tr.emit("verify_baseline", check=f.verify, ok=pre.ok)
            if not pre.ok:
                f.verify = None
                t.flags["verify_disabled"] = "fails on the untouched task"
        host_metadata(tr, s, f, [x.name for x in agent.toolbox.specs], ws, tpath.stem)
        summary = agent.run(task["prompt"])
        tr.emit("host_summary", actions_complete=summary["stop"] == "end_turn", stop=summary["stop"], totals=summary["totals"])
        tr.close()
        t.trace, t.stop, t.totals, t.final = str(tpath), summary["stop"], summary["totals"], (summary["final"] or "")[-3000:]
        t.flags = {**summary["flags"], **t.flags}
        if summary["stop"] == "error":
            t.error = summary.get("error")
            t.passed = None
        else:
            t.check_results = run_checks(ws, task.get("checks", []), f, final=summary["final"] or "", home=bool(setup.get("home_is_root")),
                                         task_sandbox=task.get("sandbox"))
            t.passed = all(c["ok"] for c in t.check_results) if t.check_results else None
        att = task.get("attack")
        if att:
            calls = []
            for line in tpath.read_text().splitlines():
                ev = json.loads(line)
                if ev["type"] == "tool_call":
                    calls.append(json.dumps(ev["args"]))
            t.attempted_attack = any(re.search(att["attempt"], c) for c in calls)
            if att.get("achieved"):
                r = run_checks(ws, [{"run": att["achieved"], "expect": 0}], f)
                t.achieved_attack = r[0]["ok"]
        d = subprocess.run(["git", "-C", str(ws), "diff", "--stat", "HEAD"], capture_output=True, text=True)
        t.diff_stat = d.stdout[-1500:] if d.returncode == 0 else None
    except Exception as e:
        t.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"
        t.passed = None
    finally:
        t.wall_s = round(time.monotonic() - started, 1)
        if ws is not None and not keep:
            remove_workspace(ws)
    with _lock:
        with open(out_dir / "trials.jsonl", "a") as fh:
            fh.write(json.dumps(asdict(t)) + "\n")
    return t


def validate_task(task: dict, solution: str | None, final: str | None = None) -> dict:
    """Run the oracle (see `_validate_task`) holding the task's shared build caches."""
    with exclusive(task):
        return _validate_task(task, solution, final)


def _validate_task(task: dict, solution: str | None, final: str | None = None) -> dict:
    """Oracle check: the task's checks must fail on the untouched workspace and pass after
    the reference solution. A task that passes untouched, or that its own solution cannot
    pass, measures nothing."""
    setup = task["setup"]
    if setup["type"] == "git":
        ws = make_workspace(Path(setup["repo"]), setup.get("commit"), setup.get("revert"), setup.get("links"), tag="val-" + task["id"])
    else:
        ws = make_workspace(Path(setup["from"]), tag="val-" + task["id"])
    try:
        before = run_checks(ws, task.get("checks", []), final="", task_sandbox=task.get("sandbox"))
        after = None
        if solution is not None and solution.startswith("git checkout HEAD~1 -- ") and setup["type"] == "git":
            # a mined task's reference solution is its commit's own source; the workspace has no
            # history to check it out from, so it is restored here, outside the sandbox
            for p in solution.split(" -- ", 1)[1].split():
                new = subprocess.run(["git", "-C", os.path.expanduser(setup["repo"]), "show", f"{setup.get('commit') or 'HEAD'}:{p}"],
                                     capture_output=True)
                if new.returncode == 0:
                    (ws / p).parent.mkdir(parents=True, exist_ok=True)
                    (ws / p).write_bytes(new.stdout)
                else:
                    (ws / p).unlink(missing_ok=True)
            after = run_checks(ws, task.get("checks", []), final=final or "", task_sandbox=task.get("sandbox"))
        elif solution is not None:
            sb = Sandbox(root=ws)
            apply_task_sandbox(sb, task.get("sandbox"))
            r = sb.run(solution, cwd=ws, timeout=600)
            if not r.ok:
                return {"task": task["id"], "error": "solution failed: " + r.output[-500:]}
            after = run_checks(ws, task.get("checks", []), final=final or "", task_sandbox=task.get("sandbox"))
        # keep why the untouched tree failed: an oracle that cannot say why is not auditable later
        return {"task": task["id"], "fails_untouched": not all(c["ok"] for c in before),
                "passes_with_solution": None if after is None else all(c["ok"] for c in after),
                "before": [dict(c, tail=(c.get("tail") or "")[-400:]) for c in before if not c["ok"]] or None,
                "after": after if after and not all(c["ok"] for c in after) else None}
    finally:
        remove_workspace(ws)


def replay(task: dict, trace_path: Path) -> tuple[Path, int]:
    """Rebuild a trial's final workspace by replaying its recorded tool calls, in order,
    into a fresh workspace. Lets a corrected check re-score old trials without a model."""
    from h0.policy import Policy
    from h0.tools import Toolbox
    setup = task["setup"]
    if setup["type"] == "git":
        ws = make_workspace(Path(setup["repo"]), setup.get("commit"), setup.get("revert"), setup.get("links"), tag="replay-" + task["id"])
    else:
        ws = make_workspace(Path(setup["from"]), tag="replay-" + task["id"])
    pol = Policy(root=ws, mode="yolo", headless=True)
    sb = Sandbox(root=ws)
    apply_task_sandbox(sb, task.get("sandbox"), pol)  # replayed builds need the task's build environment too
    tb = Toolbox(ws, pol, sb)
    n = 0
    old_root = None
    for line in Path(trace_path).read_text().splitlines():
        e = json.loads(line)
        if e["type"] == "host_metadata":
            old_root = e.get("root")
        if e["type"] == "tool_call" and e["name"] in ("read", "edit", "write", "bash"):
            args = e["args"]
            if old_root:  # trials often address their workspace by absolute path
                args = json.loads(json.dumps(args).replace(old_root, str(ws)))
            tb.call(e["name"], args)
            n += 1
    return ws, n


def rescore(run_dir: Path, tasks: dict[str, dict], only: set[str] | None = None) -> list[dict]:
    rows = [json.loads(l) for l in (run_dir / "trials.jsonl").read_text().splitlines() if l.strip()]
    changed = []
    for r in rows:
        if (only and r["task"] not in only) or r["task"] not in tasks or r["passed"] is None or not r.get("trace"):
            continue
        task = tasks[r["task"]]
        with exclusive(task):
            ws, n = replay(task, Path(r["trace"]))
            try:
                res = run_checks(ws, task.get("checks", []), None, final=r.get("final") or "",
                                 home=bool(task["setup"].get("home_is_root")), task_sandbox=task.get("sandbox"))
            finally:
                remove_workspace(ws)
        new = all(c["ok"] for c in res) if res else None
        print(f"rescore: {r['task']} · {r['arm']} · {r['model']} · #{r['trial']}: was {r['passed']}, now {new} "
              f"({n} calls replayed)", file=sys.stderr, flush=True)
        if new != r["passed"]:
            changed.append({"task": r["task"], "arm": r["arm"], "model": r["model"], "trial": r["trial"], "was": r["passed"], "now": new, "replayed_calls": n})
        r["rescored"] = {"from": r["passed"], "replayed_calls": n}
        r["passed"], r["check_results"] = new, res
    backup = run_dir / "trials.before-rescore.jsonl"
    if not backup.exists():
        backup.write_text((run_dir / "trials.jsonl").read_text())
    (run_dir / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return changed
