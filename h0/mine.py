"""Mine a project's own eval tasks from its git history (SWE-bench / SWE-smith style).

A commit that changed both unit tests and source becomes a task: check out the commit,
put the source files back to their parent's version, and ask the agent to make those
tests pass. Each candidate is kept only if the oracle holds: the tests fail on the
reverted tree and pass on the commit's own source.
"""

from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TEST_RX = re.compile(r"(^|/)(tests?|__tests__)/.*\.(test|spec)\.[jt]sx?$|\.(test|spec)\.[jt]sx?$|(^|/)tests?/test_[^/]+\.py$|(^|/)tests/[^/]+\.rs$")
SRC_RX = re.compile(r"\.(tsx?|jsx?|mjs|py|rs|go|lua)$")


def candidates(repo: Path, n: int = 400, max_src: int = 4, exclude: str = r"e2e|playwright") -> list[dict]:
    out = subprocess.run(["git", "-C", str(repo), "log", "-n", str(n), "--no-merges", "--format=@@%H%x09%s", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            if cur:
                commits.append(cur)
            h, s = line[2:].split("\t", 1)
            cur = {"commit": h, "subject": s, "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)
    ex = re.compile(exclude) if exclude else None
    found = []
    for c in commits:
        tests = [f for f in c["files"] if TEST_RX.search(f) and not (ex and ex.search(f))]
        src = [f for f in c["files"] if SRC_RX.search(f) and f not in tests and not TEST_RX.search(f) and not (ex and ex.search(f))]
        if tests and src and len(src) <= max_src:
            found.append({**c, "tests": tests, "src": src})
    return found


def message(repo: Path, commit: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B", commit], capture_output=True, text=True).stdout.strip()


def to_task(repo: Path, c: dict, project: str, test_cmd: str, links: list[str]) -> dict:
    body = message(repo, c["commit"])
    first = body.split("\n\n")[0].replace("\n", " ").strip()
    tests = " ".join(c["tests"])
    prompt = (f"{first}\n\nThe unit tests in {', '.join(c['tests'])} describe the expected behaviour and currently fail. "
              "Change the source so they pass. Do not edit the tests.")
    return {
        "id": f"{project}-{c['commit'][:8]}", "project": project, "kind": "mined",
        "prompt": prompt,
        "setup": {"type": "git", "repo": str(repo), "commit": c["commit"], "revert": c["src"], "links": links},
        "checks": [{"run": test_cmd.format(tests=tests, rust_tests=" ".join("--test " + Path(t).stem for t in c["tests"])), "timeout": 2400},
                   {"run": "git diff --quiet HEAD -- " + tests, "timeout": 30}],
        "solution": "git checkout HEAD~1 -- " + " ".join(c["src"]),
        "budget": {"max_turns": 40, "usd": 3.0},
        "source": {"commit": c["commit"], "subject": c["subject"], "src": c["src"], "tests": c["tests"]},
    }


def mine(repo: Path, project: str, test_cmd: str, links: list[str], limit: int = 12, workers: int = 4, log=print,
         sandbox: dict | None = None, subdir_filter: str | None = None) -> list[dict]:
    from h0.evals import validate_task
    cands = candidates(repo)
    log(f"mine: {len(cands)} candidate commits in {repo}")
    if subdir_filter:
        cands = [c for c in cands if all(f.startswith(subdir_filter) for f in c["tests"])]
    tasks = [to_task(repo, c, project, test_cmd, links) for c in cands]
    for t in tasks:
        if sandbox:
            t["sandbox"] = sandbox
    kept: list[dict] = []

    def check(t: dict) -> tuple[dict, dict]:
        return t, validate_task(t, t["solution"])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for t, r in ex.map(check, tasks):
            ok = r.get("fails_untouched") and r.get("passes_with_solution") and not r.get("error")
            log(f"  {'keep' if ok else 'drop'} {t['id']} · {t['source']['subject'][:70]}" + ("" if ok else f" · {json.dumps(r)[:160]}"))
            if ok:
                t["oracle"] = {"untouched_failures": r.get("before")}  # why it measures something, on record
                kept.append(t)
    return kept[:limit]
