"""h0 eval run / report.

Arms are compared on the same tasks, trial by trial. Re-running the same command
resumes: finished (task, arm, model, trial) rows in trials.jsonl are skipped.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from h0 import stats
from h0.evals import exclusive, load_tasks, run_trial


def parse_arm(spec: str) -> dict:
    name, _, fit = spec.partition("=")
    arm = {"name": name, "fitting": fit or None}
    if "nolessons" in name:
        arm["no_lessons"] = True
    if "noverify" in name:
        arm["no_verify"] = True
    if "review" in name:
        arm["finish_review"] = True
    return arm


def cmd_run(args) -> int:
    tasks = []
    for s in args.suite:
        tasks += load_tasks(Path(s))
    if args.tasks:
        keep = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    arms = [parse_arm(a) for a in args.arms.split(",")]
    for a in arms:
        if args.provider:
            a["provider"], a["base_url"] = args.provider, args.base_url
    models = args.models.split(",")
    out = Path(args.out)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    done = set()
    tj = out / "trials.jsonl"
    if tj.exists():
        for line in tj.read_text().splitlines():
            r = json.loads(line)
            if r.get("passed") is not None or not args.retry_unscored:
                done.add((r["task"], r["arm"], r["model"], r["trial"]))
    jobs = []
    for trial in range(1, args.trials + 1):
        for t in tasks:
            for m in models:
                for a in arms:
                    if (t["id"], a["name"], m, trial) not in done:
                        jobs.append((t, a, m, trial))
    meta = {"suite": args.suite, "arms": arms, "models": models, "trials": args.trials, "effort": args.effort,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": [t["id"] for t in tasks]}
    (out / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(meta, indent=2))
    print(f"eval: {len(jobs)} trials to run ({len(done)} already done) · {args.workers} workers → {out}", file=sys.stderr)
    spent = 0.0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_run_exclusive, t, a, m, n, out, args.effort, args.keep): (t["id"], a["name"], m, n) for t, a, m, n in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            key = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # run_trial records its own errors; this is a last resort
                print(f"[{i}/{len(jobs)}] {key} crashed: {e}", file=sys.stderr)
                continue
            usd = (r.totals or {}).get("usd")
            spent += usd or 0
            mark = {True: "pass", False: "FAIL", None: "unscored"}[r.passed]
            print(f"[{i}/{len(jobs)}] {r.task} · {r.arm} · {r.model} · #{r.trial}: {mark} · {r.stop} · "
                  f"${usd if usd is not None else '?'} · {r.wall_s}s · total ${spent:.2f}", file=sys.stderr)
            if args.max_usd and spent >= args.max_usd:
                print(f"eval: spend cap ${args.max_usd} reached; cancelling the rest", file=sys.stderr)
                for f in futs:
                    f.cancel()
                break
    return cmd_report(args)


def latest(rows: list[dict]) -> list[dict]:
    """One row per (task, arm, model, trial). A retried trial's scored row replaces the
    unscored row it retried, so a retry is never counted twice; a retry that also failed to
    score leaves one unscored row."""
    keep: dict[tuple, dict] = {}
    for r in rows:
        k = (r["task"], r["arm"], r["model"], r["trial"])
        if k not in keep or r.get("passed") is not None or keep[k].get("passed") is None:
            keep[k] = r
    return list(keep.values())


def summarize(rows: list[dict]) -> dict:
    rows = latest(rows)
    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["arm"])].append(r)
    table = []
    for (model, arm), rs in sorted(groups.items()):
        scored = [r for r in rs if r["passed"] is not None]
        k = sum(1 for r in scored if r["passed"])
        by_task = defaultdict(list)
        for r in sorted(scored, key=lambda r: r["trial"]):
            by_task[r["task"]].append(bool(r["passed"]))
        tot = lambda r, key: (r.get("totals") or {}).get(key)
        prompt_tokens = [((tot(r, "input_tokens") or 0) + (tot(r, "cache_read_input_tokens") or 0) + (tot(r, "cache_creation_input_tokens") or 0))
                         for r in scored if r.get("totals")]
        usd = [tot(r, "usd") for r in scored if tot(r, "usd") is not None]
        sec = [r for r in scored if r.get("attempted_attack") is not None]
        table.append({
            "model": model, "arm": arm, "trials": len(rs), "scored": len(scored), "unscored": len(rs) - len(scored),
            "passed": k, "pass_rate": round(k / len(scored), 3) if scored else None,
            "wilson95": [round(x, 3) for x in stats.wilson(k, len(scored))] if scored else None,
            "pass_hat_2": stats.pass_hat_k(by_task, 2), "pass_hat_3": stats.pass_hat_k(by_task, 3),
            "median_prompt_tokens": stats.med(prompt_tokens),
            "median_output_tokens": stats.med([tot(r, "output_tokens") for r in scored if r.get("totals")]),
            "median_turns": stats.med([tot(r, "turns") for r in scored if r.get("totals")]),
            "median_usd": stats.med(usd), "total_usd": round(sum(usd), 4) if usd else None,
            "median_wall_s": stats.med([r["wall_s"] for r in scored]),
            "median_sim_local_s": stats.med([tot(r, "sim_local_s") for r in scored if r.get("totals")]),
            "attack_attempted": (sum(1 for r in sec if r["attempted_attack"]), len(sec)) if sec else None,
            "attack_achieved": (sum(1 for r in sec if r.get("achieved_attack")), sum(1 for r in sec if r.get("achieved_attack") is not None)) if sec else None,
        })
    comparisons = []
    models = sorted({r["model"] for r in rows})
    arms = sorted({r["arm"] for r in rows})
    base = "vanilla" if "vanilla" in arms else (arms[0] if arms else None)
    for model in models:
        idx = {(r["task"], r["trial"], r["arm"]): r for r in rows if r["model"] == model and r["passed"] is not None}
        for arm in arms:
            if arm == base:
                continue
            pairs, tok_ratio, usd_diff, sim_diff = [], [], [], []
            for (task, trial, a), r in idx.items():
                if a != arm or (task, trial, base) not in idx:
                    continue
                v = idx[(task, trial, base)]
                pairs.append((bool(r["passed"]), bool(v["passed"])))
                pt = lambda x: (x["totals"].get("input_tokens", 0) + x["totals"].get("cache_read_input_tokens", 0) + x["totals"].get("cache_creation_input_tokens", 0))
                if r.get("totals") and v.get("totals") and pt(v):
                    tok_ratio.append(pt(r) / pt(v))
                    if r["totals"].get("usd") is not None and v["totals"].get("usd") is not None:
                        usd_diff.append(r["totals"]["usd"] - v["totals"]["usd"])
                    sim_diff.append(r["totals"].get("sim_local_s", 0) - v["totals"].get("sim_local_s", 0))
            if not pairs:
                continue
            m = stats.mcnemar(pairs)
            comparisons.append({"model": model, "arm": arm, "vs": base, "pairs": len(pairs), **m,
                                "median_prompt_token_ratio": stats.med(tok_ratio),
                                "mean_usd_diff": round(sum(usd_diff) / len(usd_diff), 5) if usd_diff else None,
                                "usd_diff_ci95": stats.bootstrap_ci(usd_diff),
                                "mean_sim_local_s_diff": round(sum(sim_diff) / len(sim_diff), 1) if sim_diff else None,
                                "sim_local_s_diff_ci95": stats.bootstrap_ci(sim_diff)})
    return {"table": table, "comparisons": comparisons}


def render(summary: dict, title: str) -> str:
    L = [f"# {title}", "", "| model | arm | scored | pass | 95% CI | pass^2 | med prompt tok | med out tok | med turns | med $ | total $ | med wall s | med sim-local s | attack tried | attack achieved |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    f = lambda x, d=0: "—" if x is None else (f"{x:,.{d}f}" if isinstance(x, (int, float)) else str(x))
    for r in summary["table"]:
        ci = "—" if not r["wilson95"] else f"{r['wilson95'][0]:.2f}–{r['wilson95'][1]:.2f}"
        att = "—" if not r["attack_attempted"] else f"{r['attack_attempted'][0]}/{r['attack_attempted'][1]}"
        ach = "—" if not r["attack_achieved"] else f"{r['attack_achieved'][0]}/{r['attack_achieved'][1]}"
        L.append(f"| {r['model']} | {r['arm']} | {r['scored']}{' (+' + str(r['unscored']) + ' unscored)' if r['unscored'] else ''} | "
                 f"{f(r['pass_rate'], 2)} | {ci} | {f(r['pass_hat_2'], 2)} | {f(r['median_prompt_tokens'])} | {f(r['median_output_tokens'])} | "
                 f"{f(r['median_turns'])} | {f(r['median_usd'], 4)} | {f(r['total_usd'], 2)} | {f(r['median_wall_s'], 1)} | {f(r['median_sim_local_s'], 0)} | {att} | {ach} |")
    if summary["comparisons"]:
        L += ["", "## Paired comparisons", "", "| model | arm vs base | pairs | arm only | base only | McNemar p | median prompt-token ratio | mean $ diff (95% CI) | mean sim-local s diff (95% CI) |",
              "|---|---|---|---|---|---|---|---|---|"]
        for c in summary["comparisons"]:
            ci = c["usd_diff_ci95"]
            sci = c["sim_local_s_diff_ci95"]
            L.append(f"| {c['model']} | {c['arm']} vs {c['vs']} | {c['pairs']} | {c['a_only']} | {c['b_only']} | {c['p']} | "
                     f"{f(c['median_prompt_token_ratio'], 2)} | {f(c['mean_usd_diff'], 4)} ({'—' if not ci else f'{ci[0]:.4f} to {ci[1]:.4f}'}) | "
                     f"{f(c['mean_sim_local_s_diff'], 0)} ({'—' if not sci else f'{sci[0]:.0f} to {sci[1]:.0f}'}) |")
    if summary["comparisons"]:
        # the paired noise floor: a success difference smaller than this cannot be told from chance
        L += ["", "Noise floor: with n pairs, a pass-rate difference under about 1/sqrt(n) (n = 9: ±33 points; 28: ±19; "
                  "100: ±10) is indistinguishable from run-to-run variation. Read the success columns against it."]
    return "\n".join(L) + "\n"


def cmd_report(args) -> int:
    out = Path(args.out)
    rows = [json.loads(l) for l in (out / "trials.jsonl").read_text().splitlines() if l.strip()]
    s = summarize(rows)
    (out / "summary.json").write_text(json.dumps(s, indent=2))
    md = render(s, f"Eval results — {out.name}")
    (out / "report.md").write_text(md)
    print(md)
    return 0


def cmd_validate(args) -> int:
    from h0.evals import validate_task
    sol_dir = Path(args.solutions) if args.solutions else Path(args.suite).parent / "solutions"
    bad = 0
    for t in load_tasks(Path(args.suite)):
        sol = sol_dir / f"{t['id']}.sh"
        fin = sol_dir / f"{t['id']}.final.txt"
        # a mined task carries its own reference solution (the commit's source put back)
        r = validate_task(t, sol.read_text() if sol.exists() else t.get("solution"), fin.read_text() if fin.exists() else None)
        ok = r.get("fails_untouched") and r.get("passes_with_solution") in (True, None) and not r.get("error")
        bad += 0 if ok else 1
        print(("ok   " if ok else "BAD  ") + json.dumps(r)[:600])
    return 1 if bad else 0


def cmd_mine(args) -> int:
    from h0.mine import mine
    sandbox = None
    if args.sandbox_from:
        from h0 import fitting as fit_mod
        pol = fit_mod.find(args.sandbox_from).policy or {}
        sandbox = {k: pol[k] for k in ("rw_paths", "ro_paths", "binds", "env", "resources", "read_roots") if k in pol}
    kept = mine(Path(args.repo).resolve(), args.project, args.test_cmd, args.link or [], limit=args.limit, workers=args.workers,
                log=lambda s: print(s, file=sys.stderr), sandbox=sandbox, subdir_filter=args.tests_under)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for t in kept:
            f.write(json.dumps(t) + "\n")
    print(f"mine: kept {len(kept)} oracle-valid tasks → {out}", file=sys.stderr)
    return 0


def _run_exclusive(task, *rest):
    """A trial, holding the build caches its task shares with other workspaces."""
    with exclusive(task):
        return run_trial(task, *rest)


def cmd_rescore(args) -> int:
    from h0.evals import rescore
    tasks = {}
    for suite in args.suite:
        tasks.update({t["id"]: t for t in load_tasks(Path(suite))})
    changed = rescore(Path(args.out), tasks, set(args.tasks.split(",")) if args.tasks else None)
    print(json.dumps(changed, indent=1))
    print(f"rescore: {len(changed)} trials changed outcome; original kept in trials.before-rescore.jsonl", file=sys.stderr)
    return 0


def register(sp) -> None:
    e = sp.add_parser("eval", help="run or report a paired harness eval")
    esp = e.add_subparsers(dest="eval_cmd")
    r = esp.add_parser("run")
    r.add_argument("suite", nargs="+")
    r.add_argument("--arms", default="vanilla")
    r.add_argument("--models", default="claude-haiku-5-5")
    r.add_argument("--trials", type=int, default=1)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--effort")
    r.add_argument("--tasks")
    r.add_argument("--provider")
    r.add_argument("--base-url", dest="base_url")
    r.add_argument("--max-usd", dest="max_usd", type=float)
    r.add_argument("--keep", action="store_true", help="keep workspaces")
    r.add_argument("--retry-unscored", action="store_true")
    r.add_argument("--out", required=True)
    r.set_defaults(func=cmd_run)
    mn = esp.add_parser("mine", help="mine oracle-valid tasks from a repository's git history")
    mn.add_argument("repo")
    mn.add_argument("--project", required=True)
    mn.add_argument("--test-cmd", dest="test_cmd", required=True, help="with {tests} placeholder")
    mn.add_argument("--link", action="append", help="untracked dirs to hardlink into workspaces (e.g. node_modules)")
    mn.add_argument("--limit", type=int, default=12)
    mn.add_argument("--sandbox-from", dest="sandbox_from", help="copy a fitting's build environment into every task (applies to all arms)")
    mn.add_argument("--tests-under", dest="tests_under", help="only commits whose test files live under this path")
    mn.add_argument("--workers", type=int, default=4)
    mn.add_argument("--out", required=True)
    mn.set_defaults(func=cmd_mine)
    rs = esp.add_parser("rescore", help="replay recorded tool calls and re-run (corrected) checks")
    rs.add_argument("out")
    rs.add_argument("--suite", nargs="+", required=True)
    rs.add_argument("--tasks")
    rs.set_defaults(func=cmd_rescore)
    v = esp.add_parser("validate", help="oracle-check a suite: fails untouched, passes with its solution")
    v.add_argument("suite")
    v.add_argument("--solutions")
    v.set_defaults(func=cmd_validate)
    p = esp.add_parser("report")
    p.add_argument("out")
    p.set_defaults(func=lambda a: cmd_report(type("A", (), {"out": a.out})()))
