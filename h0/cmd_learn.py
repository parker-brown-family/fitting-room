"""h0 learn propose / gate."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from h0 import fitting as fit_mod, learn


def cmd_propose(args) -> int:
    f = fit_mod.find(args.fitting)
    runs = [Path(r) for r in args.runs]
    ev = learn.gather_evidence(runs, fitted_arm=args.arm, base_arm=args.base_arm)
    if not ev:
        print("learn: no failed fitted trials; nothing to learn from", file=sys.stderr)
        return 0
    print(f"learn: {len(ev)} failure groups → {args.model}", file=sys.stderr)
    data, meta = learn.propose(f, ev, learn.task_prompts(Path(args.suite) if args.suite else None), args.model)
    g, log = learn.apply_edits(f, data.get("edits", []))
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(f.dir, out)
    g.name = args.name or f.name
    fit_mod.save(g, out)
    ch = out / "CHANGELOG.md"
    prev = ch.read_text() if ch.exists() else "# Fitting changelog\n\n"
    ch.write_text(prev + "\n" + learn.changelog_entry(g, data, log, meta, ev))
    (out / f"learn-v{g.version}.json").write_text(json.dumps({"evidence": ev, "proposal": data, "applied": log, "meta": meta}, indent=1))
    print(json.dumps({"diagnosis": data.get("diagnosis"), "applied": log, "usd": meta.get("usd")}, indent=1))
    print(f"candidate fitting v{g.version} → {out}", file=sys.stderr)
    return 0


def cmd_gate(args) -> int:
    rows = lambda d: [json.loads(l) for l in (Path(d) / "trials.jsonl").read_text().splitlines() if l.strip()]
    prev = [r for r in rows(args.prev) if r["arm"] == args.prev_arm]
    cand = [r for r in rows(args.cand) if r["arm"] == args.cand_arm]
    if args.models:
        keep = set(args.models.split(","))
        prev = [r for r in prev if r["model"] in keep]
        cand = [r for r in cand if r["model"] in keep]
    res = learn.gate(prev, cand)
    print(json.dumps(res, indent=1, default=str))
    return 0 if res["accept"] else 3


def register(sp) -> None:
    l = sp.add_parser("learn", help="propose fitting edits from eval failures, or gate a candidate")
    lsp = l.add_subparsers(dest="learn_cmd")
    p = lsp.add_parser("propose")
    p.add_argument("fitting")
    p.add_argument("--runs", nargs="+", required=True)
    p.add_argument("--suite")
    p.add_argument("--out", required=True)
    p.add_argument("--name")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--arm", default="fitted")
    p.add_argument("--base-arm", dest="base_arm", default="vanilla")
    p.set_defaults(func=cmd_propose)
    g = lsp.add_parser("gate")
    g.add_argument("--prev", required=True)
    g.add_argument("--cand", required=True)
    g.add_argument("--prev-arm", dest="prev_arm", default="fitted")
    g.add_argument("--cand-arm", dest="cand_arm", default="fitted")
    g.add_argument("--models")
    g.set_defaults(func=cmd_gate)
