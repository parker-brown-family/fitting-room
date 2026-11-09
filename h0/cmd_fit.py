"""h0 survey / h0 fit: build a fitting for one project.

The fit is one model call over the survey and the project's own instruction files,
answered as JSON. Every proposed check is then run once on the clean tree; a check
that fails before any change is never used as the verify gate, because it would trap
the agent in a failure it did not cause.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from h0 import config, fitting as fit_mod
from h0.sandbox import Sandbox
from h0.survey import AGENT_DOCS, survey, tree

FIT_PROMPT = """You are fitting a coding-agent harness to one project. The harness has a tiny generic core prompt and six tools (read, edit, write, grep, glob, bash in a no-network sandbox). Your output tailors it to THIS project only. Everything you write is loaded into every session, so every line must earn its tokens: no generic advice a competent engineer already knows, nothing true of all projects.

Return ONE JSON object and nothing else, with these keys:

"about": one sentence: what this project is.
"map_md": the project map in Markdown, at most 110 lines. Cover: what it is and who uses it; the directory layout that matters (paths, not prose); how to build, run and test it; the conventions and invariants an agent would otherwise get wrong; the traps (generated files, things never to edit, slow or networked commands, credentials). Prefer concrete paths, commands and names over adjectives. Quote the project's own rules where they are specific to it; drop the ones that are generic or about other projects.
"checks": object of named checks, each {"run": shell command, "cwd": path relative to root, "about": under 12 words, "timeout": seconds}. Only offline commands (no installs, no network). Prefer fast, targeted checks. Use the survey's candidates where they fit; you may add precise ones. At most 6.
"verify": the name of the one check the harness should run before the agent finishes: fast, broad, and expected to pass on a clean tree. null if no check is cheap and reliable enough.
"triggers": at most 6 rules bound to moments, each {"id", "on": "before:<tool>" or "after:<tool>", "match": a Python regex over the command or path, "action": "inject" | "block" | "require", "check": a check name for require, "note": under 30 words}. Use them for project rules that matter at a specific moment: editing a generated file, touching a sensitive directory, committing, running something slow.
"policy": {"read_roots": [], "write_roots": [], "ro_paths": [], "rw_paths": [], "env": {}} — extra absolute paths the sandbox needs (for example a shared build cache), or empty lists.
"lessons": at most 6 short project-specific lessons distilled from the project's own docs, each {"id": "L1"..., "text": under 30 words}.
"sources": the file paths you relied on most.

PROJECT SURVEY (static, generated):
{survey}

DIRECTORY TREE (depth 2):
{tree}

PROJECT INSTRUCTION FILES:
{docs}
"""


def _docs(root: Path, budget: int = 90_000, extra: list[str] | None = None) -> str:
    parts: list[str] = []
    used = 0
    for x in extra or []:
        p = Path(os.path.expanduser(x))
        if p.is_file():
            text = p.read_text(errors="replace")[: max(0, budget - used)]
            parts.append(f"===== {p} =====\n{text}")
            used += len(text)
    for d in AGENT_DOCS + [".claude/CLAUDE.md", "docs/README.md", "app/AGENTS.md", "app/README.md"]:
        p = root / d
        if not p.is_file():
            continue
        text = p.read_text(errors="replace")
        room = budget - used
        if room <= 2000:
            break
        if len(text) > room:
            text = text[:room] + f"\n… [truncated: {len(text) - room} more characters]"
        parts.append(f"===== {d} =====\n{text}")
        used += len(text)
    for m in ("package.json", "Cargo.toml", "app/Cargo.toml", "pyproject.toml"):
        p = root / m
        if p.is_file() and used < budget:
            text = p.read_text(errors="replace")[:6000]
            parts.append(f"===== {m} =====\n{text}")
            used += len(text)
    return "\n\n".join(parts) or "(no instruction files)"


def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the reply")
    return json.loads(m.group(0))


def validate_checks(root: Path, checks: dict, policy: dict, timeout_cap: float = 900) -> dict:
    sb = Sandbox(root=root, rw_paths=[Path(os.path.expanduser(p)) for p in policy.get("rw_paths", [])],
                 ro_paths=[Path(os.path.expanduser(p)) for p in policy.get("ro_paths", [])],
                 env={k: str(v) for k, v in (policy.get("env") or {}).items()}, resources=policy.get("resources") or {},
                 binds=policy.get("binds") or [])
    out = {}
    for name, c in checks.items():
        r = sb.run(c["run"], cwd=root / c.get("cwd", "."), timeout=min(float(c.get("timeout", 600)), timeout_cap))
        out[name] = {"baseline": "timeout" if r.timed_out else ("pass" if r.ok else "fail"), "ms": r.ms,
                     "tail": r.output[-600:]}
    return out


def build_fitting(root: Path, name: str, model: str, effort: str | None, validate: bool = True, log=print,
                  docs: list[str] | None = None, brief: str | None = None) -> fit_mod.Fitting:
    from h0.providers import make
    root = Path(os.path.realpath(root))
    s = survey(root)
    prompt = (FIT_PROMPT.replace("{survey}", json.dumps(s, indent=1)[:40_000])
              .replace("{tree}", tree(root)).replace("{docs}", _docs(root, extra=docs)))
    if brief:
        prompt = "CONTEXT FROM THE PERSON FITTING THIS HARNESS:\n" + brief + "\n\n" + prompt
    settings = config.Settings(model=model, effort=effort, max_tokens=32_000, headless=True)
    provider = make(settings)
    log(f"fit: surveyed {s['files']} files; asking {model} ({len(prompt):,} characters)")
    turn = provider.complete("You produce precise JSON configuration for a coding-agent harness.",
                             [{"role": "user", "content": prompt}], [], settings)
    text = "".join(b.get("text", "") for b in turn.blocks if b.get("type") == "text")
    try:
        data = _parse_json(text)
    except (ValueError, json.JSONDecodeError):
        turn = provider.complete("You produce precise JSON configuration for a coding-agent harness.",
                                 [{"role": "user", "content": prompt},
                                  {"role": "assistant", "content": [{"type": "text", "text": text[:2000]}]},
                                  {"role": "user", "content": "That was not one valid JSON object. Reply with only the JSON object."}],
                                 [], settings)
        text = "".join(b.get("text", "") for b in turn.blocks if b.get("type") == "text")
        data = _parse_json(text)
    cost = config.price(model, turn.usage)
    checks = {k: {kk: vv for kk, vv in v.items() if kk in ("run", "cwd", "about", "timeout", "net")}
              for k, v in (data.get("checks") or {}).items()}
    policy = data.get("policy") or {}
    policy = {k: v for k, v in policy.items() if v}
    f = fit_mod.Fitting(name=name, root=root, about=data.get("about", ""), checks=checks,
                        triggers=[t for t in (data.get("triggers") or []) if t.get("on") and t.get("match")],
                        policy=policy, verify=data.get("verify"), map_md=data.get("map_md", ""),
                        lessons=[{"id": l.get("id", f"L{i+1}"), "text": l["text"], "helpful": 0, "harmful": 0, "source": "fit"}
                                 for i, l in enumerate(data.get("lessons") or []) if l.get("text")])
    report = {"model": model, "usage": turn.usage, "usd": cost, "survey_files": s["files"], "built": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if validate and checks:
        from h0.evals import make_workspace, remove_workspace
        log(f"fit: running {len(checks)} checks on a clean clone (never in the real working tree)")
        ws = make_workspace(root, links=["node_modules"], tag=f"fit-{name}")
        try:
            baseline = validate_checks(ws, checks, policy)
        finally:
            remove_workspace(ws)
        report["baseline"] = baseline
        for n, b in baseline.items():
            log(f"  {n}: {b['baseline']} ({b['ms']} ms)")
        if f.verify and baseline.get(f.verify, {}).get("baseline") != "pass":
            log(f"fit: verify check {f.verify!r} does not pass on a clean tree; verify disabled")
            f.verify = None
    sources = []
    for p in data.get("sources") or []:
        fp = root / p
        if fp.is_file():
            sources.append({"path": p, "sha256": hashlib.sha256(fp.read_bytes()).hexdigest()})
    f._report, f._sources, f._survey = report, sources, s
    return f


def write_fitting(f: fit_mod.Fitting, out: Path) -> Path:
    d = fit_mod.save(f, out)
    (d / "sources.json").write_text(json.dumps(getattr(f, "_sources", []), indent=2) + "\n")
    (d / "fit-report.json").write_text(json.dumps(getattr(f, "_report", {}), indent=2) + "\n")
    (d / "survey.json").write_text(json.dumps(getattr(f, "_survey", {}), indent=1) + "\n")
    return d


def cmd_survey(args) -> int:
    print(json.dumps(survey(Path(args.dir)), indent=1))
    return 0


def cmd_fit(args) -> int:
    root = Path(args.dir).resolve()
    name = args.name or root.name
    out = Path(args.out) if args.out else config.DATA / "fittings" / name
    f = build_fitting(root, name, args.model, args.effort, validate=not args.no_validate,
                      log=lambda s: print(s, file=sys.stderr), docs=args.doc, brief=args.brief)
    d = write_fitting(f, out)
    r = f._report
    usd = "?" if r.get("usd") is None else f"${r['usd']:.3f}"
    print(f"fitting {name} → {d} · map {len(f.map_md.splitlines())} lines · {len(f.checks)} checks · "
          f"{len(f.triggers)} triggers · {len(f.lessons)} lessons · verify {f.verify} · {usd}")
    return 0


def register(sp) -> None:
    s = sp.add_parser("survey", help="print a static survey of a project")
    s.add_argument("dir")
    s.set_defaults(func=cmd_survey)
    f = sp.add_parser("fit", help="build a fitting for a project")
    f.add_argument("dir")
    f.add_argument("--name")
    f.add_argument("--out")
    f.add_argument("--model", default="claude-opus-5-5")
    f.add_argument("--effort", default="high")
    f.add_argument("--no-validate", action="store_true")
    f.add_argument("--doc", action="append", default=[], help="extra instruction file to fit from (repeatable)")
    f.add_argument("--brief", help="a paragraph of context about what the fitting is for")
    f.set_defaults(func=cmd_fit)
