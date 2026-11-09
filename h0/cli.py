"""h0 command line."""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import sys
import time
from pathlib import Path

from h0 import __version__, config, context, fitting as fit_mod, trace as trace_mod
from h0.loop import Agent
from h0.policy import Policy
from h0.providers import ProviderError, make as make_provider
from h0.sandbox import Sandbox
from h0.tools import Toolbox
from h0.ui import UI


def session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + pysecrets.token_hex(3)


def messages_path(sid: str) -> Path:
    return config.STATE / "sessions" / f"{sid}.messages.json"


def save_messages(sid: str, agent) -> None:
    """The transcript, verbatim (thinking blocks and their signatures included, which the API
    requires back unchanged), so a session can be resumed by a fresh harness."""
    p = messages_path(sid)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"session": sid, "root": str(agent.toolbox.root), "messages": agent.messages}, f)
    os.replace(tmp, p)


def load_messages(sid: str) -> dict:
    return json.loads(messages_path(sid).read_text())


def build_agent(settings: config.Settings, root: Path, fit, ui: UI, trace, provider=None, extra_tools=None) -> Agent:
    pol_cfg = (fit.policy if fit else {}) or {}
    policy = Policy(root=root,
                    read_roots=[Path(os.path.expanduser(p)) for p in pol_cfg.get("read_roots", [])],
                    write_roots=[Path(os.path.expanduser(p)) for p in pol_cfg.get("write_roots", [])],
                    allow_secret_paths=pol_cfg.get("allow_secret_paths", []),
                    mode=settings.mode, headless=settings.headless)
    sandbox = Sandbox(root=Path(os.path.realpath(root)),
                      rw_paths=[Path(os.path.expanduser(p)) for p in pol_cfg.get("rw_paths", [])],
                      ro_paths=[Path(os.path.expanduser(p)) for p in pol_cfg.get("ro_paths", [])],
                      env={k: str(v) for k, v in (pol_cfg.get("env") or {}).items()},
                      mask=[Path(os.path.expanduser(p)) for p in pol_cfg.get("mask", [])],
                      resources=pol_cfg.get("resources") or {}, binds=pol_cfg.get("binds") or [],
                      allow_nested_git=bool(pol_cfg.get("allow_nested_git")))
    protected = [d for d in fit_mod.search_paths() if d.exists()]
    if fit and fit.dir:
        protected.append(Path(fit.dir))
    policy.protected = protected
    from h0 import judge as judge_mod
    if judge_mod.enabled(pol_cfg):
        policy.judge = judge_mod.judge
    sandbox.protect = protected
    enabled = (fit.tools if fit and fit.tools else None)
    if settings.local and not enabled:
        enabled = ["read", "edit", "write", "bash", "grep", "check"]
    tb = Toolbox(root, policy, sandbox, output_chars=settings.tool_output_chars,
                 checks=(fit.checks if fit else {}), ask=ui.ask, emit=trace.emit, enabled=enabled, local=settings.local)
    system = context.build(Path(os.path.realpath(root)), fit)
    prov = provider or make_provider(settings)
    return Agent(settings=settings, provider=prov, toolbox=tb, policy=policy, trace=trace, ui=ui, system=system, fitting=fit)


def resolve_settings(args, fit) -> config.Settings:
    s = config.Settings()
    if fit:
        s.model = fit.model or s.model
        s.effort = fit.effort or s.effort
        for k, v in (fit.budgets or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
    env_model = os.environ.get("H0_MODEL")
    if env_model:
        s.model = env_model
    for name in ("model", "effort", "provider", "base_url", "max_turns", "budget_usd", "max_tokens"):
        v = getattr(args, name, None)
        if v is not None:
            setattr(s, name, v)
    if getattr(args, "yolo", False):
        s.mode = "yolo"
    if getattr(args, "plan", False):
        s.mode = "plan"
    if s.provider == "openai" or getattr(args, "local", False):
        s.apply_local_profile()
    return s


def pick_fitting(args, cwd: Path):
    if getattr(args, "no_fitting", False):
        return None
    if getattr(args, "fitting", None):
        return fit_mod.find(args.fitting)
    return fit_mod.resolve(cwd)


def host_metadata(trace, settings, fit, tb_names, root, sid):
    trace.emit("host_metadata", host="harness-0", host_version=__version__, session_id=sid, provider=settings.provider,
               configured_model_id=settings.model, requested_effort=settings.effort, tools=tb_names,
               loaded_skills=[f"fitting:{fit.name}@v{fit.version}"] if fit else [], fitting=fit.name if fit else None,
               root=str(root), sandbox="bubblewrap", mode=settings.mode)


def cmd_run(args) -> int:
    config.ensure_dirs()
    cwd = Path(args.cwd or os.getcwd()).resolve()
    fit = pick_fitting(args, cwd)
    root = Path(args.root).resolve() if args.root else (fit.root if fit and str(cwd).startswith(str(fit.root)) else cwd)
    settings = resolve_settings(args, fit)
    settings.headless = bool(args.prompt) or args.json
    resumed = None
    if getattr(args, "resume", None):
        resumed = load_messages(args.resume)
        root = Path(resumed["root"])
    sid = args.resume or session_id()
    tpath = Path(args.trace) if args.trace else config.STATE / "sessions" / f"{sid}.jsonl"
    trace = trace_mod.Trace(tpath)
    ui = UI(quiet=args.json or args.quiet, show_thinking=args.thinking)
    try:
        agent = build_agent(settings, root, fit, ui, trace)
    except ProviderError as e:
        print(f"h0: {e}", file=sys.stderr)
        return 2
    if resumed:
        agent.messages = resumed["messages"]
        trace.emit("resumed", messages=len(agent.messages))
    host_metadata(trace, settings, fit, [s.name for s in agent.toolbox.specs], root, sid)
    if args.prompt:
        prompt = args.prompt if args.prompt != "-" else sys.stdin.read()
        summary = agent.run(prompt)
        save_messages(sid, agent)
        summary.update(session=sid, trace=str(tpath), model=settings.model, effort=settings.effort,
                       fitting=fit.name if fit else None)
        trace.emit("host_summary", actions_complete=summary["stop"] == "end_turn", stop=summary["stop"], totals=summary["totals"])
        trace.close()
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            if not summary["final"].endswith("\n"):
                print()
            t = summary["totals"]
            usd = "?" if t["usd"] is None else f"${t['usd']:.4f}"
            print(f"\n· {summary['stop']} · {t['turns']} turns · {t['input_tokens'] + t['cache_read_input_tokens'] + t['cache_creation_input_tokens']:,} in "
                  f"({t['cache_read_input_tokens']:,} cached) · {t['output_tokens']:,} out · {usd} · {summary['wall_s']}s", file=sys.stderr)
            print(f"· trace {tpath}", file=sys.stderr)
        return 0 if summary["stop"] == "end_turn" else 1
    return repl(agent, settings, fit, trace, tpath, sid)


def repl(agent, settings, fit, trace, tpath, sid) -> int:
    try:
        import readline  # noqa: F401  (line editing and history)
    except ImportError:
        pass
    fit_label = f"{fit.name} v{fit.version}" if fit else "none"
    print(f"h0 {__version__} · {settings.model} · effort {settings.effort or 'default'} · fitting {fit_label} · /help")
    print(f"root {agent.toolbox.root}")
    while True:
        try:
            line = input("\n› ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line:
            continue
        if line in ("/q", "/quit", "/exit"):
            break
        if line == "/help":
            print("/cost  /model <id>  /effort <level>  /trace  /quit")
            continue
        if line == "/cost":
            print(json.dumps(agent.totals.as_dict(), indent=2))
            continue
        if line.startswith("/model "):
            settings.model = line.split(None, 1)[1]
            print(f"model {settings.model}")
            continue
        if line.startswith("/effort "):
            settings.effort = line.split(None, 1)[1]
            print(f"effort {settings.effort}")
            continue
        if line == "/trace":
            print(tpath)
            continue
        try:
            summary = agent.run(line)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            continue
        finally:
            save_messages(sid, agent)
        t = summary["totals"]
        usd = "?" if t["usd"] is None else f"${t['usd']:.4f}"
        print(f"\n\033[2m· {summary['stop']} · {t['turns']} turns · {usd} so far\033[0m")
    trace.emit("host_summary", actions_complete=True, stop="interactive", totals=agent.totals.as_dict())
    trace.close()
    print(f"trace {tpath}")
    print(f"resume with: h0 --resume {sid}")
    return 0


def cmd_trace(args) -> int:
    p = Path(args.file)
    if args.action == "verify":
        ok, bad, n = trace_mod.verify(p)
        print(f"{'ok' if ok else 'BROKEN'} · {n} events" + ("" if ok else f" · first bad seq {bad}"))
        return 0 if ok else 1
    for line in p.read_text().splitlines():
        ev = json.loads(line)
        t = ev["type"]
        if t == "tool_call":
            print(f"{ev['seq']:>4} ⏺ {ev['name']} {json.dumps(ev['args'])[:160]}")
        elif t == "tool_result":
            print(f"{ev['seq']:>4}   {'✓' if ev['ok'] else '✗'} {ev['ms']} ms {ev['text'][:120]!r}")
        elif t == "assistant":
            print(f"{ev['seq']:>4} ◆ turn {ev['turn']} {ev['stop_reason']} {ev['text'][:160]!r}")
        else:
            print(f"{ev['seq']:>4} · {t} " + json.dumps({k: v for k, v in ev.items() if k not in ('seq', 'ts', 'type', 'prev', 'hash')})[:200])
    return 0


def cmd_doctor(args) -> int:
    from h0 import secrets
    rows = []
    rows.append(("python", sys.version.split()[0], sys.version_info >= (3, 11)))
    rows.append(("bubblewrap", "found" if Sandbox.available() else "missing", Sandbox.available()))
    if Sandbox.available():
        probe = ("test ! -e $HOME/.ssh && test ! -e $HOME/.config/gh && test ! -e $HOME/.local/share/keyrings "
                 "&& test ! -S /run/user/$(id -u)/bus && ! (exec 3<>/dev/tcp/1.1.1.1/443) 2>/dev/null && echo isolated")
        r = Sandbox(root=Path.cwd()).run(probe, timeout=20)
        rows.append(("sandbox isolation", r.output.strip() or f"exit {r.exit}", "isolated" in r.output))
    key = secrets.get("anthropic")
    rows.append(("anthropic key", "present (keyring or env)" if key else "missing", bool(key)))
    om = context.omarchy_version()
    rows.append(("omarchy", om or "not detected", True))
    fits = fit_mod.all_fittings()
    rows.append(("fittings", ", ".join(f.name for f in fits) or "none", True))
    here = fit_mod.resolve(Path.cwd())
    rows.append(("fitting for cwd", here.name if here else "none", True))
    for name, value, ok in rows:
        print(f"{'✓' if ok else '✗'} {name:<18} {value}")
    return 0 if all(ok for _, _, ok in rows) else 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sub_names = {"run", "fit", "eval", "learn", "trace", "doctor", "host", "omarchy", "survey"}
    if not argv or argv[0] not in sub_names and argv[0] not in ("-h", "--help", "--version"):
        argv = ["run"] + argv
    p = argparse.ArgumentParser(prog="h0", description="harness-0: a project-fitted, security-first coding agent")
    p.add_argument("--version", action="version", version=f"h0 {__version__}")
    sp = p.add_subparsers(dest="cmd")

    r = sp.add_parser("run", help="interactive session, or one task with -p")
    r.add_argument("-p", "--prompt", help="run one task headless ('-' reads stdin)")
    r.add_argument("--model")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--provider", choices=["anthropic", "openai"])
    r.add_argument("--base-url", dest="base_url")
    r.add_argument("--local", action="store_true", help="local-model profile: fewer tools, tighter output")
    r.add_argument("--fitting", help="fitting name or directory (default: resolved from cwd)")
    r.add_argument("--no-fitting", action="store_true")
    r.add_argument("--root", help="project root (default: fitting root or cwd)")
    r.add_argument("--cwd")
    r.add_argument("--yolo", action="store_true", help="allow risky actions inside the sandbox without asking")
    r.add_argument("--plan", action="store_true", help="read-only")
    r.add_argument("--max-turns", dest="max_turns", type=int)
    r.add_argument("--max-tokens", dest="max_tokens", type=int)
    r.add_argument("--budget-usd", dest="budget_usd", type=float)
    r.add_argument("--json", action="store_true", help="print a JSON summary (implies headless)")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--thinking", action="store_true", help="show thinking summaries")
    r.add_argument("--trace", help="trace file path")
    r.add_argument("--resume", help="continue a saved session by id (its root and transcript are restored)")
    r.set_defaults(func=cmd_run)

    t = sp.add_parser("trace", help="verify or show a session trace")
    t.add_argument("action", choices=["verify", "show"])
    t.add_argument("file")
    t.set_defaults(func=cmd_trace)

    d = sp.add_parser("doctor", help="check the sandbox, key and fittings")
    d.set_defaults(func=cmd_doctor)

    for extra in ("fit", "survey", "eval", "learn", "host", "omarchy"):
        try:
            mod = __import__(f"h0.cmd_{extra}", fromlist=["register"])
            mod.register(sp)
        except ImportError:
            pass

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args)
