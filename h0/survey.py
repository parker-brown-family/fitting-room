"""Static project survey: what a fitting is built from. No model calls.

Git runs inside the sandbox, because a repository's own config can name commands
(core.fsmonitor) that git runs on the host otherwise.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from h0.redact import is_secret_path
from h0.sandbox import Sandbox

LANG = {".rs": "Rust", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
        ".mjs": "JavaScript", ".py": "Python", ".go": "Go", ".lua": "Lua", ".sh": "Shell", ".css": "CSS",
        ".html": "HTML", ".md": "Markdown", ".toml": "TOML", ".json": "JSON", ".qml": "QML", ".swift": "Swift",
        ".kt": "Kotlin", ".dart": "Dart", ".rb": "Ruby", ".c": "C", ".h": "C", ".cpp": "C++", ".zig": "Zig",
        ".conf": "Config", ".yml": "YAML", ".yaml": "YAML", ".sql": "SQL", ".svg": "SVG"}
AGENT_DOCS = ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".github/copilot-instructions.md",
              "CONTRIBUTING.md", "README.md", "SECURITY.md", "SOUL.md", "MEMORY.md"]
MANIFESTS = ["package.json", "Cargo.toml", "pyproject.toml", "go.mod", "Makefile", "justfile", "deno.json",
             "vitest.config.ts", "playwright.config.ts", "tsconfig.json", "pubspec.yaml", "build.gradle"]


def _git(sb: Sandbox, args: str, cwd: Path) -> str:
    r = sb.run(f"git --no-pager {args}", cwd=cwd, timeout=60)
    return r.output if r.ok else ""


def survey(root: Path) -> dict:
    root = Path(os.path.realpath(root))
    sb = Sandbox(root=root)
    is_git = (root / ".git").exists()
    files: list[str] = []
    if is_git:
        files = [f for f in _git(sb, "ls-files -co --exclude-standard", root).splitlines() if f]
    if not files:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in {".git", "node_modules", "target", "dist", "build", ".next", ".venv"}]
            files += [str(Path(dp, f).relative_to(root)) for f in fns]
            if len(files) > 50_000:
                break
    exts = Counter(Path(f).suffix.lower() for f in files)
    langs = Counter()
    for e, n in exts.items():
        if e in LANG:
            langs[LANG[e]] += n
    top_dirs = Counter(f.split("/", 1)[0] for f in files if "/" in f)
    out: dict = {
        "root": str(root), "name": root.name, "files": len(files),
        "languages": langs.most_common(10), "top_dirs": top_dirs.most_common(25),
        "secret_paths": sorted({f for f in files if is_secret_path(f)})[:50],
        "agent_docs": {}, "manifests": {}, "checks": {},
    }
    for d in AGENT_DOCS:
        p = root / d
        if p.is_file():
            text = p.read_text(errors="replace")
            out["agent_docs"][d] = {"lines": text.count("\n") + 1, "bytes": len(text.encode())}
    for m in MANIFESTS:
        hits = [f for f in files if f == m or f.endswith("/" + m)]
        hits = [h for h in hits if "node_modules" not in h][:6]
        if hits:
            out["manifests"][m] = hits
    cands: dict[str, dict] = {}
    for pj in out["manifests"].get("package.json", []):
        try:
            scripts = json.loads((root / pj).read_text()).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            continue
        cwd = str(Path(pj).parent)
        runner = "npm run" if not (root / cwd / "bun.lockb").exists() and not (root / cwd / "bun.lock").exists() else "bun run"
        for s in ("test", "lint", "typecheck", "type-check", "check", "build"):
            if s in scripts:
                cmd = scripts[s]
                if s == "test" and "vitest" in cmd and "run" not in cmd:
                    full = f"npx vitest run"
                else:
                    full = f"{runner} {s}"
                cands[f"{s}" if cwd == "." else f"{s}:{cwd}"] = {"run": full, "cwd": cwd, "from": f"{pj} scripts.{s}: {cmd}"[:200]}
    for ct in out["manifests"].get("Cargo.toml", []):
        cwd = str(Path(ct).parent)
        tag = "" if cwd == "." else f":{cwd}"
        cands[f"cargo-check{tag}"] = {"run": "cargo check --offline --quiet", "cwd": cwd, "from": ct}
        cands[f"cargo-test{tag}"] = {"run": "cargo test --offline --quiet", "cwd": cwd, "from": ct}
        cands[f"clippy{tag}"] = {"run": "cargo clippy --offline --quiet -- -D warnings", "cwd": cwd, "from": ct}
        cands[f"fmt{tag}"] = {"run": "cargo fmt --check", "cwd": cwd, "from": ct}
    if "pyproject.toml" in out["manifests"] or any(f.startswith("tests/test_") and f.endswith(".py") for f in files):
        cands["pytest"] = {"run": "python3 -m pytest -q", "cwd": ".", "from": "pyproject/tests"}
    if (root / "Makefile").exists():
        targets = re.findall(r"^([a-zA-Z][\w-]*):", (root / "Makefile").read_text(errors="replace"), re.M)
        for t in ("test", "check", "lint"):
            if t in targets:
                cands[f"make-{t}"] = {"run": f"make {t}", "cwd": ".", "from": "Makefile"}
    out["checks"] = cands
    if is_git:
        log = _git(sb, "log --format=%h%x09%cs%x09%s -n 60", root)
        out["recent_commits"] = [l.split("\t", 2) for l in log.splitlines() if l.count("\t") == 2][:60]
        count = _git(sb, "rev-list --count HEAD", root).strip()
        out["commit_count"] = int(count) if count.isdigit() else None
        churn = Counter()
        for line in _git(sb, "log --format= --name-only -n 400", root).splitlines():
            if line.strip():
                churn[line.strip()] += 1
        out["hot_files"] = churn.most_common(30)
        out["branch"] = _git(sb, "rev-parse --abbrev-ref HEAD", root).strip() or None
    else:
        out["recent_commits"], out["commit_count"], out["hot_files"], out["branch"] = [], None, [], None
    return out


def tree(root: Path, depth: int = 2, cap: int = 220) -> str:
    root = Path(root)
    lines: list[str] = []
    skip = {".git", "node_modules", "target", "dist", "build", ".next", ".venv", "__pycache__", ".cache"}

    def walk(d: Path, level: int) -> None:
        if level > depth or len(lines) >= cap:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            return
        for p in entries:
            if p.name in skip or (p.name.startswith(".") and p.name not in {".github", ".claude", ".cursor"}):
                continue
            lines.append("  " * level + p.name + ("/" if p.is_dir() else ""))
            if len(lines) >= cap:
                return
            if p.is_dir():
                walk(p, level + 1)

    walk(root, 0)
    return "\n".join(lines)
