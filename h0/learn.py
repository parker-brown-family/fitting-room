"""The learning loop: evidence → proposed fitting edits → candidate → eval gate.

1. Evidence. Every failed fitted trial, with its failed checks, the agent's tool calls,
   the triggers that fired, and — the most informative part — what the paired vanilla
   trial did on the same task when it passed.
2. Proposal. One model call returns a small set of typed edits to the fitting. It is
   told to state general rules and never the answer to a specific task: a fitting that
   memorises its eval suite has learned nothing.
3. Candidate. The edits are applied to a copy, the version is bumped, and a changelog
   entry records the evidence and the reasoning.
4. Gate. The candidate is accepted only if an eval run shows no lower pass rate and no
   regression on any task the previous version passed. The gate runs separately (h0
   eval), so its numbers are the same kind as every other result.

Lesson counters follow ACE (arXiv 2510.04618): a lesson injected into a passing trial
gains `helpful`, into a failing one gains `harmful`; lessons whose harm exceeds their
help drop out of the prompt layer.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from h0 import config, fitting as fit_mod

REFLECT_PROMPT = """You maintain a fitting: the project-specific layer of a coding-agent harness. The harness ran an eval twice per task — once WITH this fitting ("fitted") and once WITHOUT it ("vanilla"), same model. Below are the fitted failures, each paired with what the vanilla run did on the same task.

Your job: propose the smallest set of edits to the fitting that would have prevented these failures WITHOUT hurting the tasks that already pass. Prefer changing or deleting a rule that misled the agent over adding new text. Every token you add is loaded on every turn of every session.

Hard rules:
- State general rules about the project, true for many requests. Never encode the answer to a specific task (no specific key combinations, file contents, values or wording taken from the task prompts).
- If the failure was caused by the fitting itself (a trigger, lesson or map line that pushed the agent the wrong way), fix or remove that item.
- If the evidence does not support a confident change, propose nothing for that failure and say why.

Return ONE JSON object:
{"diagnosis": [{"task": id, "cause": "fitting" | "model" | "check" | "unclear", "explanation": one or two sentences}],
 "edits": [ one of:
   {"op": "update_trigger", "id": trigger id, "note": new note, "match": optional new regex, "action": optional},
   {"op": "remove_trigger", "id": trigger id},
   {"op": "add_trigger", "trigger": {"id", "on", "match", "action", "note", "check"?}},
   {"op": "edit_map", "old": exact text in the map, "new": replacement},
   {"op": "add_lesson", "text": under 30 words},
   {"op": "remove_lesson", "id": lesson id} ],
 "expected_effect": one sentence}

CURRENT FITTING
fitting.toml triggers and checks:
{triggers}

map.md:
{map}

lessons:
{lessons}

EVIDENCE
{evidence}
"""


def _trace_summary(path: str, limit: int = 30) -> list[str]:
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return ["(trace unavailable)"]
    for line in lines:
        e = json.loads(line)
        t = e["type"]
        if t == "tool_call":
            out.append(f"call {e['name']} {json.dumps(e['args'])[:300]}")
        elif t == "trigger":
            out.append(f"trigger {e.get('rule')} {e.get('action')} ok={e.get('ok')}")
        elif t == "verify":
            out.append(f"verify {e.get('check')} ok={e.get('ok')}")
        elif t == "policy_denied":
            out.append(f"denied {e.get('rule')}: {e.get('reason', '')[:120]}")
    return out[-limit:]


def gather_evidence(run_dirs: list[Path], fitted_arm: str = "fitted", base_arm: str = "vanilla", max_items: int = 12) -> list[dict]:
    rows = []
    for d in run_dirs:
        rows += [json.loads(l) for l in (d / "trials.jsonl").read_text().splitlines() if l.strip()]
    idx = {(r["task"], r["model"], r["trial"], r["arm"]): r for r in rows}
    items = []
    for r in rows:
        if r["arm"] != fitted_arm or r["passed"] is not False:
            continue
        v = idx.get((r["task"], r["model"], r["trial"], base_arm))
        items.append({
            "task": r["task"], "model": r["model"], "trial": r["trial"],
            "failed_checks": [(c.get("run") or c.get("final_regex"))[:300] + " → " + (c.get("tail") or "")[-300:]
                              for c in r["check_results"] if not c["ok"]],
            "fitted_steps": _trace_summary(r["trace"]) if r.get("trace") else [],
            "fitted_final": (r.get("final") or "")[-600:],
            "vanilla_passed": None if v is None else v["passed"],
            "vanilla_steps": _trace_summary(v["trace"], 15) if v and v.get("trace") else [],
        })
    # one item per task-model is usually enough; keep the evidence small
    seen, out = set(), []
    for it in items:
        k = (it["task"], it["model"])
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out[:max_items]


def task_prompts(suite: Path | None) -> dict[str, str]:
    if not suite:
        return {}
    from h0.evals import load_tasks
    return {t["id"]: t["prompt"] for t in load_tasks(suite)}


def propose(f: fit_mod.Fitting, evidence: list[dict], prompts: dict[str, str], model: str = "claude-opus-5-5") -> tuple[dict, dict]:
    from h0.providers import make
    triggers = fit_mod.dump_toml({"checks": f.checks, "triggers": f.triggers, "verify": f.verify})
    ev = []
    for it in evidence:
        ev.append({**it, "task_prompt": prompts.get(it["task"], "(unknown)")[-400:]})
    prompt = (REFLECT_PROMPT.replace("{triggers}", triggers).replace("{map}", f.map_md)
              .replace("{lessons}", json.dumps(f.lessons, indent=1)).replace("{evidence}", json.dumps(ev, indent=1)))
    s = config.Settings(model=model, effort="high", max_tokens=24_000, headless=True)
    turn = make(s).complete("You improve coding-agent harness configuration from evidence. You reply with JSON only.",
                            [{"role": "user", "content": prompt}], [], s)
    text = "".join(b.get("text", "") for b in turn.blocks if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {"diagnosis": [], "edits": [], "error": "no JSON"}
    return data, {"usage": turn.usage, "usd": config.price(model, turn.usage)}


def apply_edits(f: fit_mod.Fitting, edits: list[dict]) -> tuple[fit_mod.Fitting, list[str]]:
    g = copy.deepcopy(f)
    log = []
    for e in edits:
        op = e.get("op")
        if op == "update_trigger":
            for t in g.triggers:
                if t.get("id") == e.get("id"):
                    for k in ("note", "match", "action", "check"):
                        if e.get(k):
                            t[k] = e[k]
                    log.append(f"updated trigger {e['id']}")
                    break
            else:
                log.append(f"skipped: no trigger {e.get('id')}")
        elif op == "remove_trigger":
            n = len(g.triggers)
            g.triggers = [t for t in g.triggers if t.get("id") != e.get("id")]
            log.append(f"removed trigger {e.get('id')}" if len(g.triggers) < n else f"skipped: no trigger {e.get('id')}")
        elif op == "add_trigger":
            t = e.get("trigger") or {}
            if t.get("on") and t.get("match"):
                g.triggers.append(t)
                log.append(f"added trigger {t.get('id')}")
        elif op == "edit_map":
            if e.get("old") and e["old"] in g.map_md:
                g.map_md = g.map_md.replace(e["old"], e.get("new", ""), 1)
                log.append("edited map")
            else:
                log.append("skipped: map text not found")
        elif op == "add_lesson":
            n = 1 + max([int(l["id"][1:]) for l in g.lessons if re.fullmatch(r"L\d+", l.get("id", ""))] or [0])
            g.lessons.append({"id": f"L{n}", "text": e["text"], "helpful": 0, "harmful": 0, "source": "learn"})
            log.append(f"added lesson L{n}")
        elif op == "remove_lesson":
            n = len(g.lessons)
            g.lessons = [l for l in g.lessons if l.get("id") != e.get("id")]
            log.append(f"removed lesson {e.get('id')}" if len(g.lessons) < n else f"skipped: no lesson {e.get('id')}")
    g.version = f.version + 1
    return g, log


def update_counters(f: fit_mod.Fitting, run_dirs: list[Path], arm: str = "fitted") -> None:
    """ACE-style counters: every lesson present in a fitted trial is credited with that
    trial's outcome. Crude (a lesson present is not a lesson used) and labelled as such."""
    outcomes = []
    for d in run_dirs:
        for l in (d / "trials.jsonl").read_text().splitlines():
            r = json.loads(l)
            if r["arm"] == arm and r["passed"] is not None:
                outcomes.append(r["passed"])
    for lesson in f.lessons:
        lesson["helpful"] = lesson.get("helpful", 0) + sum(1 for o in outcomes if o)
        lesson["harmful"] = lesson.get("harmful", 0) + sum(1 for o in outcomes if not o)
        lesson["counter_method"] = "present-in-trial (not attributed)"


def gate(prev_rows: list[dict], cand_rows: list[dict]) -> dict:
    """Accept only if the candidate's pass rate is not lower and no task the previous
    version passed in every scored trial now fails in any."""
    def by_task(rows):
        d = defaultdict(list)
        for r in rows:
            if r["passed"] is not None:
                d[(r["task"], r["model"])].append(bool(r["passed"]))
        return d
    p, c = by_task(prev_rows), by_task(cand_rows)
    rate = lambda d: (sum(sum(v) for v in d.values()) / max(1, sum(len(v) for v in d.values()))) if d else None
    regressions = [k for k, v in p.items() if all(v) and k in c and not all(c[k])]
    fixed = [k for k, v in p.items() if not all(v) and k in c and all(c[k])]
    pr, cr = rate(p), rate(c)
    accept = cr is not None and pr is not None and cr >= pr and not regressions
    return {"prev_rate": pr, "cand_rate": cr, "regressions": regressions, "fixed": fixed, "accept": accept}


def changelog_entry(f_new, diagnosis: dict, log: list[str], meta: dict, evidence: list[dict]) -> str:
    lines = [f"## v{f_new.version} — {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Proposed by the learning loop from {len(evidence)} failed fitted trials (cost ${(meta.get('usd') or 0):.3f}).", ""]
    for d in diagnosis.get("diagnosis", []):
        lines.append(f"- **{d.get('task')}** — cause: {d.get('cause')}. {d.get('explanation', '')}")
    lines += ["", "Edits applied:"] + [f"- {x}" for x in log]
    if diagnosis.get("expected_effect"):
        lines += ["", f"Expected effect: {diagnosis['expected_effect']}"]
    return "\n".join(lines) + "\n"
