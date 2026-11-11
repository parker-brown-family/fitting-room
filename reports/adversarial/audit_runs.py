#!/usr/bin/env python3
"""Read archived trial records without replaying commands or calling models.

Only numeric accounting, identifiers and content hashes enter the output. Prompts,
final answers and traces remain in the source archive. Overlapping run snapshots
are reported separately, never summed as independent evidence or global spend.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from h0.cmd_eval import latest
from h0.stats import binom_two_sided, bootstrap_ci


def sums(rows, field):
    values = [(r.get("totals") or {}).get(field) for r in rows]
    known = [x for x in values if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return {"known_sum": sum(known) if known else None,
            "known_records": len(known), "unknown_records": len(values) - len(known)}


def task_sign_flip(task_deltas):
    """Sensitivity analysis: flip whole task clusters, not repeated trials.

    This adds an exchangeability assumption; it does not establish it. All-zero
    tasks have no sign and are removed before enumeration. No optional stopping.
    """
    nonzero = [x for x in task_deltas if x != 0]
    if not nonzero:
        return {"p": 1.0, "method": "exact", "nonzero_tasks": 0}
    threshold = abs(sum(nonzero)) - 1e-12
    if len(nonzero) <= 16:
        values = [abs(sum(s * v for s, v in zip(signs, nonzero))) for signs in itertools.product((-1, 1), repeat=len(nonzero))]
        return {"p": sum(x >= threshold for x in values) / len(values), "method": "exact",
                "nonzero_tasks": len(nonzero), "assignments": len(values)}
    rng = random.Random(20261110)
    n = 20000
    extreme = sum(abs(sum(rng.choice((-1, 1)) * v for v in nonzero)) >= threshold for _ in range(n))
    return {"p": (extreme + 1) / (n + 1), "method": "monte-carlo-plus-one",
            "nonzero_tasks": len(nonzero), "assignments": n}


def audit(path):
    data = path.read_bytes()
    rows = []
    bad = []
    for n, raw in enumerate(data.decode().splitlines(), 1):
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
            if not isinstance(r, dict) or not all(k in r for k in ("task", "model", "arm", "trial")):
                raise ValueError("missing trial identity")
            rows.append(r)
        except (ValueError, TypeError) as exc:
            bad.append({"line": n, "error": str(exc)})
    reduced = latest(rows)
    groups = []
    for model, arm in sorted({(r["model"], r["arm"]) for r in rows}):
        attempts = [r for r in rows if (r["model"], r["arm"]) == (model, arm)]
        rs = [r for r in reduced if (r["model"], r["arm"]) == (model, arm)]
        scored = [r for r in rs if r.get("passed") is not None]
        unscored = [r for r in rs if r.get("passed") is None]
        groups.append({"model": model, "arm": arm, "attempt_rows": len(attempts),
            "latest_rows": len(rs), "scored": len(scored), "unscored": len(unscored),
            "all_attempts_usd": sums(attempts, "usd"),
            "latest_all_usd": sums(rs, "usd"), "report_scored_only_usd": sums(scored, "usd"),
            "latest_unscored_usd": sums(unscored, "usd"),
            "latest_unscored_wall_s": sum(r["wall_s"] for r in unscored if r.get("wall_s") is not None) if unscored else None})
    comparisons = []
    for model in sorted({r["model"] for r in reduced}):
        arms = sorted({r["arm"] for r in reduced if r["model"] == model})
        if "vanilla" not in arms:
            continue
        for arm in arms:
            if arm == "vanilla":
                continue
            idx = {(r["task"], r["trial"], r["arm"]): r for r in reduced if r["model"] == model and r.get("passed") is not None}
            tasks = defaultdict(list)
            pair_deltas = []
            a_only = b_only = 0
            for (task, trial, a), r in idx.items():
                if a != arm or (task, trial, "vanilla") not in idx:
                    continue
                other = idx[task, trial, "vanilla"]
                delta = int(bool(r["passed"])) - int(bool(other["passed"]))
                pair_deltas.append(delta)
                tasks[task].append(delta)
                a_only += delta == 1
                b_only += delta == -1
            if not pair_deltas:
                continue
            means = [sum(v) / len(v) for v in tasks.values()]
            comparisons.append({"model": model, "arm": arm, "base": "vanilla", "pairs": len(pair_deltas),
                "unique_tasks": len(tasks), "arm_only": a_only, "base_only": b_only,
                "paired_trial_mcnemar_p": binom_two_sided(a_only, a_only + b_only),
                "equal_task_weighted_success_delta": sum(means) / len(means),
                "task_cluster_bootstrap_ci95": bootstrap_ci(means, iters=4000, seed=20261110),
                "task_sign_flip_sensitivity": task_sign_flip(means),
                "task_deltas": {k: v for k, v in sorted(tasks.items())}})
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "malformed": bad,
            "groups": groups, "comparisons": comparisons}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("archive-audit.json"))
    args = parser.parse_args()
    paths = sorted(args.root.glob("*/trials.jsonl"))
    if not paths:
        parser.error("no immediate-child trial archives found")
    audits = [audit(p) for p in paths]
    doc = {"schema": "h0-archive-audit-v1", "paid_model_calls": 0,
        "scope": "Immediate child runs only; rescoring subdirectories excluded. Archives may overlap; do not aggregate them.",
        "cluster_inference": "Task-weighted bootstrap and sign-flip sensitivity; sign exchangeability and independent task sampling are assumptions, not established facts.",
        "runs": audits}
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Audited {len(audits)} archives; no commands replayed or model calls made.")
    for run in audits:
        for group in run["groups"]:
            omitted = group["latest_unscored_usd"]["known_sum"]
            if omitted:
                print(f"{Path(run['path']).parent.name}: {group['model']} / {group['arm']}: scored-only cost excludes ${omitted:.4f} in {group['unscored']} unscored records")
    return int(any(r["malformed"] for r in audits))


if __name__ == "__main__":
    raise SystemExit(main())
