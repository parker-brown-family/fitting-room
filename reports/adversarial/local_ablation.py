#!/usr/bin/env python3
"""Exploratory five-arm h0 pilot on an already installed, local Ollama model.

Uses h0's existing provider and agent loop. Only a literal loopback URL is
allowed; proxy and redirect handling cannot route a request to a paid endpoint.
No model download, no service startup, no paid credentials, no production edit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["H0_JUDGE"] = "off"

from h0.cli import build_agent, host_metadata
from h0.config import Settings
from h0.evals import QuietUI
from h0.fitting import Fitting, Triggers
from h0.providers.openai_compat import OpenAICompat
from h0.sandbox import Sandbox
from h0.trace import Trace

URL = "http://127.0.0.1:11434/v1"
ARMS = ("none", "map", "rules", "lessons", "full")

TASKS = [
    {"id": "precedence", "file": "engine/settings/resolve.py",
     "prompt": "A per-user setting of false or zero is replaced by the site's value. Fix configuration precedence so a supplied user value is retained, including false, zero, and an empty string. A key absent from user settings should use the site value. Keep the public function signature.",
     "broken": "def resolve(user, site, key):\n    return user.get(key) or site.get(key)\n",
     "reference": "def resolve(user, site, key):\n    return user[key] if key in user else site.get(key)\n",
     "oracle": "import json; from engine.settings.resolve import resolve; print(json.dumps([resolve({'x':False},{'x':True},'x'),resolve({'x':0},{'x':9},'x'),resolve({'x':''},{'x':'a'},'x'),resolve({},{'x':7},'x'),resolve({'x':None},{'x':9},'x')]))",
     "expected": [False, 0, "", 7, None]},
    {"id": "intervals", "file": "engine/windows/combine.py",
     "prompt": "The scheduling service loses a long reservation when a shorter overlapping reservation arrives. Correct reservation merging. Intervals are half-open integer pairs; touching endpoints are contiguous and should merge. Return a sorted list without mutating the caller's list. Empty input should return an empty list.",
     "broken": "def merge(intervals):\n    intervals.sort()\n    out=[]\n    for start,end in intervals:\n        if out and start <= out[-1][1]:\n            out[-1]=(out[-1][0],end)\n        else:\n            out.append((start,end))\n    return out\n",
     "reference": "def merge(intervals):\n    out=[]\n    for start,end in sorted(intervals):\n        if out and start <= out[-1][1]:\n            out[-1]=(out[-1][0],max(out[-1][1],end))\n        else:\n            out.append((start,end))\n    return out\n",
     "oracle": "import json; from engine.windows.combine import merge; x=[(5,8),(1,10),(2,3),(10,12)]; old=x.copy(); print(json.dumps([merge(x), x==old, merge([]), merge([(4,5),(1,2)]), merge([(0,10),(2,4),(6,8)])]))",
     "expected": [[[1, 12]], True, [], [[1, 2], [4, 5]], [[0, 10]]]},
]


class LoopbackOnly(urllib.request.HTTPHandler):
    def http_open(self, request):
        if request.host != "127.0.0.1:11434":
            raise RuntimeError("Refusing a non-local inference endpoint")
        return super().http_open(request)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Inference endpoint redirects are forbidden")


def prepare(root, task, reference=False):
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for p in ("engine", "engine/settings", "engine/windows"):
        (root / p).mkdir(exist_ok=True)
        (root / p / "__init__.py").write_text("")
    (root / task["file"]).write_text(task["reference"] if reference else task["broken"])
    (root / "README.md").write_text("# Fixture service\nPython standard library. Production code is under engine/.\n")
    for n in range(18):
        (root / "engine" / f"adapter_{n:02d}.py").write_text(f"# Adapter {n}; no scheduling or settings logic here.\nNAME = 'adapter-{n}'\n")


def score(root, task):
    # The oracle is a command owned by this experiment, never a file in the
    # model's writable workspace. It executes only after the agent is finished.
    import shlex
    r = Sandbox(root).run("python3 -B -c " + shlex.quote(task["oracle"]), timeout=10)
    try:
        value = json.loads(r.output)
        # JSON equality in Python conflates False and 0. Canonical JSON strings
        # keep these values distinct in the actual oracle.
        correct = json.dumps(value, sort_keys=True) == json.dumps(task["expected"], sort_keys=True)
    except (ValueError, TypeError):
        value, correct = None, False
    return {"passed": r.ok and correct, "exit": r.exit, "value": value, "output": r.output[-1500:]}


def fitting(root, arm):
    if arm == "none":
        return None
    f = Fitting(name="fixture-service", root=root)
    if arm in ("map", "full"):
        f.map_md = "Python service. engine/settings/resolve.py owns configuration precedence. engine/windows/combine.py owns reservation interval merging. engine/adapter_*.py are unrelated adapter metadata. There are no third-party dependencies."
    if arm in ("lessons", "full"):
        f.lessons = [{"id": "edge-cases", "text": "Boundary values and mutation of caller-owned containers have caused regressions here. Exercise them when changing behavior.", "helpful": 1, "harmful": 0}]
    if arm in ("rules", "full"):
        f.triggers = [{"on": "after:read", "match": r".*\.py", "action": "inject",
                       "note": "For this service, verify boundary values and preserve the caller's input containers. Run a small behavioral check after editing."}]
    return f


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="omarchy-4b-base:latest")
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("local-pilot"))
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()
    if not 1 <= args.turns <= 8 or not 128 <= args.max_tokens <= 4096:
        parser.error("turns must be 1..8 and max-tokens 128..4096")
    args.out.mkdir(parents=True, exist_ok=True)
    for arm in ARMS:
        f = fitting(Path("/fixture"), arm)
        if arm in ("rules", "full"):
            notes = Triggers(f.triggers).after("read", {"path": "engine/settings/resolve.py"})
            if len(notes) != 1 or not notes[0].strip():
                raise RuntimeError("Rule ablation did not inject its note; no inference will run")
    # A dedicated opener ignores HTTP_PROXY and rejects redirects. The provider
    # remains h0.providers.openai_compat.OpenAICompat without modifications.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LoopbackOnly(), NoRedirect())
    urllib.request.install_opener(opener)
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
        installed = json.load(response)["models"]
    selected = next((m for m in installed if m["name"] == args.model), None)
    if selected is None:
        parser.error("requested model is not already installed; downloads are forbidden")
    prereg = {"schema": "h0-local-pilot-v1", "model": args.model, "model_digest": selected["digest"],
        "provider_url": URL, "arms": ARMS, "tasks": [{k:v for k,v in t.items() if k != "reference"} for t in TASKS],
        "max_turns": args.turns, "max_tokens": args.max_tokens, "seed": 20261110,
        "confirmatory": False, "repetitions": 1,
        "limits": "Two authored toy tasks, one trial per arm, shared local server; author saw tasks and fitting. Cannot estimate general benefit or frontier ceiling. First inference may include model load. No cache control.",
        "paid_api_calls": 0}
    pre = args.out / "preregistration.json"
    if pre.exists():
        parser.error("output already has a preregistration; choose a new output directory")
    pre.write_text(json.dumps(prereg, indent=2) + "\n")
    (args.out / "source.sha256").write_text(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + "\n")
    validations = []
    with tempfile.TemporaryDirectory(prefix="h0-local-oracle-", dir=Path.home()/".cache") as d:
        for task in TASKS:
            broken, fixed = Path(d)/f"{task['id']}-broken", Path(d)/f"{task['id']}-fixed"
            prepare(broken, task); prepare(fixed, task, True)
            before, after = score(broken, task), score(fixed, task)
            validations.append({"task": task["id"], "before": before, "reference": after})
    (args.out / "oracle-validation.json").write_text(json.dumps(validations, indent=2) + "\n")
    if any(x["before"]["passed"] or not x["reference"]["passed"] for x in validations):
        raise RuntimeError("Oracle validation failed; no inference will run")
    jobs = [(task, arm) for task in TASKS for arm in ARMS]
    random.Random(20261110).shuffle(jobs)
    rows = []
    for sequence, (task, arm) in enumerate(jobs):
        started = time.monotonic()
        tr = Trace(args.out / f"{sequence:02d}-{task['id']}-{arm}.jsonl")
        try:
            with tempfile.TemporaryDirectory(prefix="h0-local-pilot-", dir=Path.home()/".cache") as d:
                root = Path(d) / "repo"
                prepare(root, task)
                s = Settings(model=args.model, provider="openai", base_url=URL, max_tokens=args.max_tokens,
                             max_turns=args.turns, headless=True)
                s.apply_local_profile()
                f = fitting(root, arm)
                agent = build_agent(s, root, f, QuietUI(), tr, provider=OpenAICompat(URL))
                agent.toolbox.sandbox.resources = {"memory_max": "1G", "cpu_weight": 20, "nice": 19}
                host_metadata(tr, s, f, [x.name for x in agent.toolbox.specs], root, str(sequence))
                summary = agent.run(task["prompt"])
                outcome = score(root, task)
                tr.emit("host_summary", stop=summary["stop"], totals=summary["totals"])
                row = {"task": task["id"], "arm": arm, "sequence": sequence, "stop": summary["stop"],
                       "totals": summary["totals"], "oracle": outcome,
                       "passed": outcome["passed"] if summary["stop"] != "error" else None,
                       "error": summary.get("error")}
        except Exception as exc:
            row = {"task": task["id"], "arm": arm, "sequence": sequence, "passed": None,
                   "error": type(exc).__name__ + ": " + str(exc)}
        finally:
            tr.close()
        row["wall_s"] = round(time.monotonic() - started, 3)
        rows.append(row)
        with (args.out / "trials.jsonl").open("a") as out:
            out.write(json.dumps(row) + "\n")
        print(json.dumps({k:row.get(k) for k in ("sequence","task","arm","passed","stop","wall_s","error")}), flush=True)
    (args.out / "summary.json").write_text(json.dumps({"preregistration_sha256": hashlib.sha256(pre.read_bytes()).hexdigest(),
        "rows": rows, "paid_api_calls": 0, "local_inference_trials": len(rows)}, indent=2) + "\n")
    return int(any(r["passed"] is None for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
