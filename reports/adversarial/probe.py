#!/usr/bin/env python3
"""Offline falsification probes. No provider, credential lookup, or network calls.

Run from any cwd: python3 reports/adversarial/probe.py --out results.json
Exit 0 = experiments completed, 1 = falsified claims, 2 = blocked/error probes.
The results preserve failures; this is not a production test-suite replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ["H0_JUDGE"] = "off"

from h0 import trace
from h0.cmd_eval import summarize
from h0.evals import run_checks
from h0.sandbox import Sandbox
from h0.policy import Policy
from h0.tools import Toolbox
from h0.undo import TreeUndo


class Blocked(Exception):
    pass


def git(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


def shell(sb, command):
    result = sb.run(command, timeout=10)
    if not result.ok:
        raise Blocked(f"sandbox command failed: exit={result.exit}; {result.output[-1500:]}")
    return result


def sandbox_secrets(base):
    root = base / "repo"
    root.mkdir()
    git(root)
    token = "synthetic-canary-" + base.name
    names = [".env", "src/.env", "src/a/b/c/.env", "src/a/b/c/d/.env", "node_modules/pkg/.env"]
    for name in names:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(token)
    sb = Sandbox(root)
    shell(sb, "printf SANDBOX_READY")
    observations = []
    for name in names:
        r = shell(sb, "cat " + shlex.quote(name) + "; rc=$?; printf '\\nREAD_EXIT=%s\\n' \"$rc\"")
        observations.append({"path": name, "canary_visible": token in r.output, "read_output": r.output})
    # A concurrent host operation creates this after construction; it is not a
    # claim that an agent can invent a secret it did not already know.
    (root / "late").mkdir()
    (root / "late/.env").write_text(token)
    observations.append({"path": "late/.env (created after construction)",
                         "canary_visible": token in shell(sb, "cat late/.env").output})
    policy = Policy(root, mode="yolo", headless=True, judge=None)
    tools = Toolbox(root, policy, sb)
    # The instruction names a directory and reads its sole member; the command
    # never spells a credential-shaped name. It uses the complete tool path,
    # including policy and redaction. The only data is our synthetic canary.
    e2e = tools.call("bash", {"command": "python3 -c 'from pathlib import Path; print(next(Path(\"node_modules/pkg\").iterdir()).read_text())'"})
    flags_after_read = policy.flags.as_dict()
    network_verdict = policy.check_exec("printf offline-policy-check", net=True)
    control = all(not r["canary_visible"] for r in observations[:3])
    return control and not any(r["canary_visible"] for r in observations), {
        "control_top_and_depth4_masked": control, "observations": observations,
        "full_tool_path": {"ok": e2e.ok, "canary_visible": token in e2e.text,
                           "flags_after_read": flags_after_read,
                           "subsequent_network_policy_verdict": network_verdict.verdict},
        "scope": "Synthetic repository secrets only; no host credentials read or network command run."}


def sandbox_nested_git(base):
    root = base / "repo"
    root.mkdir()
    git(root)
    existing = root / "vendor/existing"
    existing.mkdir(parents=True)
    git(existing)
    sb = Sandbox(root)
    shell(sb, "true")
    paths = ["new/.git", "node_modules/pkg/.git", "a/b/c/d/e/f/g/.git"]
    command = "; ".join("mkdir -p " + shlex.quote(p) + "; printf '[core]\\n  fsmonitor = false\\n' > " +
                        shlex.quote(p + "/config") for p in paths)
    r = shell(sb, command)
    observations = [{"path": p, "survives": (root / p).exists()} for p in paths]
    cfg = existing / ".git/config"
    before = cfg.read_bytes()
    r2 = sb.run("printf '\\n[core]\\n  fsmonitor = false\\n' >> vendor/existing/.git/config", timeout=10)
    mutable = cfg.read_bytes() != before
    control = not observations[0]["survives"] and "new/.git" in r.integrity
    return control and not any(x["survives"] for x in observations) and not mutable, {
        "control_shallow_repo_removed": control, "observations": observations,
        "existing_nested_config_mutable": mutable, "write_exit": r2.exit,
        "reported_integrity": r.integrity,
        "scope": "Inert fsmonitor=false configuration; no host hook execution."}


def trace_integrity(base):
    path = base / "session.jsonl"
    t = trace.Trace(path)
    t.emit("host_metadata", root="synthetic")
    t.emit("tool_call", name="write", args={"path": "a", "content": "before"})
    t.emit("host_summary", stop="end_turn")
    t.close()
    lines = path.read_text().splitlines(keepends=True)
    mutations = {
        "untouched": "".join(lines),
        "edit_middle": "".join(lines).replace('"content": "before"', '"content": "after"'),
        "delete_middle": lines[0] + lines[2],
        "partial_final_line": "".join(lines)[:-5],
        "delete_summary": "".join(lines[:-1]),
        "keep_metadata_only": lines[0],
        "empty_closed_trace": "",
    }
    outcomes = {}
    for name, text in mutations.items():
        # Same path and original anchors.log retained throughout.
        path.write_text(text)
        outcomes[name] = list(trace.verify(path))
    anchor = (base / "anchors.log").read_text()
    control = outcomes["untouched"][0] and all(not outcomes[x][0] for x in
                ("edit_middle", "delete_middle", "partial_final_line"))
    return control and all(not outcomes[x][0] for x in ("delete_summary", "keep_metadata_only", "empty_closed_trace")), {
        "controls_detect_edit_interior_delete_partial_line": control, "verify_results": outcomes,
        "original_anchor_preserved": anchor,
        "scope": "Detectable against existing anchor, but verify() does not consult it. No claim of cryptographic forgery."}


def undo_metadata(base):
    outcomes = {}
    for spoof in (False, True):
        case = base / ("spoof" if spoof else "control")
        root = case / "root"
        root.mkdir(parents=True)
        p = root / "config.txt"
        p.write_bytes(b"before!")
        undo = TreeUndo([root], case / "store")
        undo.checkpoint()
        old = p.stat()
        p.write_bytes(b"after!!")
        if spoof:
            os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))
        else:
            os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000))
        current = p.stat()
        changes = undo.changes()
        result = undo.undo()
        outcomes["metadata_preserved" if spoof else "ordinary_change"] = {
            "same_inode": old.st_ino == current.st_ino,
            "same_size": old.st_size == current.st_size,
            "same_mtime_ns": old.st_mtime_ns == current.st_mtime_ns,
            "reported_changes": changes, "undo_result": result,
            "restored_original_bytes": p.read_bytes() == b"before!"}
    return all(v["restored_original_bytes"] for v in outcomes.values()), outcomes


def undo_empty_directory(base):
    root = base / "root"
    root.mkdir()
    empty = root / "empty"
    empty.mkdir()
    undo = TreeUndo([root], base / "store")
    undo.checkpoint()
    empty.rmdir()
    result = undo.undo()
    return empty.is_dir(), {"directory_restored": empty.is_dir(), "undo_result": result,
                            "scope": "Existing empty directory removed by a request."}


def eval_oracle(base):
    root = base / "repo"
    root.mkdir()
    git(root)
    (root / "answer.py").write_text("def answer(): return 0\n")
    (root / "check.py").write_text("from answer import answer\nassert answer() == 42\n")
    checks = [{"run": "python3 check.py", "timeout": 10}]
    spec = {"resources": {"nice": 19}}
    before = run_checks(root, checks, task_sandbox=spec)
    if not before or before[0]["exit"] != 1 or "AssertionError" not in before[0].get("tail", ""):
        raise Blocked("Expected oracle assertion failure; got " + repr(before))
    shell(Sandbox(root), "printf 'raise SystemExit(0)\\n' > check.py")
    after = run_checks(root, checks, task_sandbox=spec)
    unchanged_bug = (root / "answer.py").read_text() == "def answer(): return 0\n"
    return not (unchanged_bug and after[0]["ok"]), {
        "before": before, "after_agent_rewrites_check_file": after,
        "buggy_implementation_unchanged": unchanged_bug,
        "scope": "run_checks permits workspace-based oracles; not evidence that a historical model run cheated."}


def eval_costs(base):
    def row(arm, trial, passed, usd, wall, tokens):
        return {"task": "synthetic", "project": "synthetic", "kind": "authored", "model": "offline",
                "arm": arm, "trial": trial, "passed": passed, "wall_s": wall,
                "totals": {"usd": usd, "input_tokens": tokens, "output_tokens": 1, "turns": 1}}
    rows = [row("fitted", 1, True, .01, 1, 10), row("fitted", 2, None, 10, 100, 10000),
            row("vanilla", 1, True, .02, 2, 20)]
    result = summarize(rows)
    fitted = next(x for x in result["table"] if x["arm"] == "fitted")
    actual = sum(x["totals"]["usd"] for x in rows if x["arm"] == "fitted")
    return abs(fitted["total_usd"] - actual) < 1e-9, {
        "input_rows": rows, "reported_summary": result, "actual_recorded_fitted_usd": actual,
        "scope": "Synthetic monetary records; zero paid calls made. Tests accounting, not model quality."}


PROBES = [
    ("sandbox-secret-coverage", "Repository secret files stay hidden from shell tools", sandbox_secrets),
    ("sandbox-nested-git", "Commands cannot plant or modify git configuration for later host execution", sandbox_nested_git),
    ("trace-anchored-completeness", "Verification detects removal of events from an anchored closed trace", trace_integrity),
    ("undo-metadata-spoof", "Tree undo detects and restores modified bytes despite preserved metadata", undo_metadata),
    ("undo-empty-directory", "Tree undo restores deleted empty directories", undo_empty_directory),
    ("eval-oracle-tampering", "Passing an eval oracle requires repairing the implementation", eval_oracle),
    ("eval-failure-costs", "Total eval cost includes recorded spend in unscored trials", eval_costs),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results.json"))
    parser.add_argument("--only", choices=[x[0] for x in PROBES])
    args = parser.parse_args()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, capture_output=True, check=True).stdout.strip()
    results = []
    # Accidental Python HTTP/provider use fails closed. Probes never instantiate
    # providers. Sandbox commands have network namespaces and use no net=True.
    with patch.object(socket.socket, "connect", side_effect=RuntimeError("network disabled for adversarial probes")):
        for id_, claim, probe in PROBES:
            if args.only and args.only != id_:
                continue
            started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(prefix="h0-adversarial-", dir=Path.home() / ".cache") as tmp:
                    protected, evidence = probe(Path(tmp))
                status = "corroborated" if protected else "falsified"
            except Blocked as exc:
                status, evidence = "blocked", {"error": str(exc)}
            except Exception as exc:
                status, evidence = "error", {"type": type(exc).__name__, "error": str(exc)}
            result = {"id": id_, "claim": claim, "status": status,
                      "wall_s": round(time.monotonic() - started, 3), "evidence": evidence}
            results.append(result)
            print(f"{status:12} {id_}", flush=True)
    document = {"schema": "h0-adversarial-v1", "tested_revision": revision,
                "python": sys.version, "platform": platform.platform(),
                "paid_model_calls": 0, "production_changes": False,
                "results": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n")
    return 2 if any(r["status"] in ("blocked", "error") for r in results) else int(any(r["status"] == "falsified" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
