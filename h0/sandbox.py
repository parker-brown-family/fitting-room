"""bubblewrap sandbox for every shell command the model runs.

The view from inside:
  /               read-only host
  $HOME           an empty tmpfs; only toolchains are re-bound into it
  <project>       read-write, except .git/hooks and .git/config (read-only: either
                  one is a way to run code outside the sandbox the next time a person
                  uses git)
  /run/user/$UID  an empty tmpfs: no session bus, no keyring, no Wayland
  /tmp            fresh tmpfs
  network         none, unless the call is granted egress
Toolchain caches that tools insist on writing (cargo locks, bun) sit under a tmpfs
overlay, so writes succeed and vanish when the command ends.
"""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()

OVERLAY_DIRS = [".cargo", ".rustup", ".bun"]
RO_DIRS = [".local/share/mise", ".config/mise", ".local/bin", ".config/git", ".cache/ms-playwright", ".deno"]
RO_FILES = [".gitconfig"]
SYSTEM_PATH = ["/usr/local/bin", "/usr/bin", "/usr/share/omarchy/bin"]
OUTPUT_CAP = 4_000_000
CREDENTIAL_FILES = [".cargo/credentials", ".cargo/credentials.toml", ".config/git/credentials", ".config/mise/credentials"]
SKIP_SCAN = {".git", "node_modules", "target", "dist", "build", ".next", ".venv", "venv", "__pycache__", ".cache"}
NESTED_GIT_DEPTH = 6        # directory levels below the root searched for a planted .git
NESTED_GIT_CAP = 20_000     # directory entries examined per scan, so the check stays cheap


@dataclass
class Result:
    ok: bool
    exit: int | None
    output: str
    ms: int
    timed_out: bool = False
    truncated_bytes: int = 0
    integrity: list = field(default_factory=list)   # files outside the sandbox's reach that a command tried to plant


@dataclass
class Sandbox:
    root: Path
    rw_paths: list[Path] = field(default_factory=list)
    ro_paths: list[Path] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    mask: list[Path] = field(default_factory=list)
    resources: dict = field(default_factory=dict)   # memory_max ("8G"), cpu_weight (1-10000), nice (0-19)
    binds: list = field(default_factory=list)        # [src, dest] read-only; dest may use {root}, e.g. "{root}/../zed-upstream"
    protect: list[Path] = field(default_factory=list)  # paths inside the project kept read-only (for example the fittings that set policy)
    allow_nested_git: bool = False   # a project that clones or initialises repositories inside itself opts in
    enabled: bool = True

    def __post_init__(self) -> None:
        # Everything the mount table depends on is read ONCE, before the model can touch the
        # project. Re-reading git's files per command let a command rewrite them (an
        # alternates entry naming ~/.ssh, a .git file naming $HOME) and have the next command
        # mount what it named. Found by h0's own self-review, 2026-11-08.
        self.root = Path(os.path.realpath(self.root))
        self._git = self._compute_git_binds()
        self._secret_masks = self._scan_secret_files()
        self._sentinels = self._snapshot_sentinels()
        self._nested_git = self._scan_nested_git()

    @staticmethod
    def available() -> bool:
        return shutil.which("bwrap") is not None

    # Files that run code on the host the next time a person uses the project, but that a
    # read-only bind cannot cover because they need not exist yet: git's commondir redirect
    # (git then takes config and hooks from wherever it points) and direnv's .envrc.
    def _sentinel_paths(self) -> list[Path]:
        return [self.root / ".git" / "commondir", self.root / ".envrc"]

    def _snapshot_sentinels(self) -> dict:
        snap = {}
        for p in self._sentinel_paths():
            snap[str(p)] = p.read_bytes() if p.is_file() and not p.is_symlink() else None
        return snap

    def _restore_sentinels(self) -> list[str]:
        changed = []
        for path, before in self._sentinels.items():
            p = Path(path)
            now = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            exists = p.exists() or p.is_symlink()
            if now == before and (exists == (before is not None)):
                continue
            changed.append(str(p.relative_to(self.root)))
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            if before is not None:
                p.write_bytes(before)
        return changed

    # A repository planted inside the project: `sub/.git` (a directory, or a file naming a
    # gitdir) with a hostile config runs its core.fsmonitor the next time a person uses git
    # in `sub/`. Nested .git entries present at construction (submodule checkouts, vendored
    # repositories) are the person's; any other is removed after each command.
    def _scan_nested_git(self, depth: int = NESTED_GIT_DEPTH, cap: int = NESTED_GIT_CAP) -> set[str]:
        found: set[str] = set()
        seen = 0
        for dirpath, dirnames, filenames in os.walk(self.root):  # never follows symlinks
            rel = Path(dirpath).relative_to(self.root)
            for name in dirnames + filenames:  # a symlink named .git lands in one of the two
                seen += 1
                if name == ".git" and rel.parts:  # the top-level .git is pinned by its own binds
                    found.add(str(rel / name))
            dirnames[:] = [] if len(rel.parts) >= depth else [d for d in dirnames if d not in SKIP_SCAN]
            if seen > cap:
                break
        return found

    @staticmethod
    def _force_remove(p: Path) -> None:
        if p.is_symlink() or not p.is_dir():
            p.unlink(missing_ok=True)
            return
        # a command can chmod 000 a directory inside so rmtree stops partway; open every
        # directory up first (top-down, so each is fixed before the walk descends into it)
        os.chmod(p, 0o700)
        for dirpath, dirnames, _ in os.walk(p):
            for d in dirnames:
                q = os.path.join(dirpath, d)
                if not os.path.islink(q):
                    os.chmod(q, 0o700)
        shutil.rmtree(p)

    def _remove_nested_git(self) -> list[str]:
        removed = []
        for rel in sorted(self._scan_nested_git() - self._nested_git):
            try:
                self._force_remove(self.root / rel)
                removed.append(rel)
            except OSError as e:
                removed.append(f"{rel} (could not remove: {e.strerror or e})")
        return removed

    @staticmethod
    def _is_gitdir(p: Path) -> bool:
        return (p / "HEAD").is_file() and (p / "refs").is_dir() and (p / "objects").is_dir()

    def _scan_secret_files(self, depth: int = 4, cap: int = 20_000) -> list[Path]:
        from h0.redact import is_secret_path
        found, seen = [], 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_depth = len(Path(dirpath).relative_to(self.root).parts)
            dirnames[:] = [] if rel_depth >= depth else [d for d in dirnames if d not in SKIP_SCAN]
            for f in filenames:
                seen += 1
                if is_secret_path(f) and not Path(dirpath, f).is_symlink():
                    found.append(Path(dirpath, f))
            if seen > cap:
                break
        return found

    def _compute_git_binds(self) -> list[str]:
        args: list[str] = []
        dotgit = self.root / ".git"
        common: Path | None = None
        gitdir: Path | None = None
        if dotgit.is_dir() and not dotgit.is_symlink():
            common = gitdir = dotgit
            # .git bound onto itself becomes a mount point: it cannot be renamed or replaced
            # with a file pointing somewhere a later `git status` on the host would trust.
            args += ["--bind", str(dotgit), str(dotgit)]
        elif dotgit.is_file():
            text = dotgit.read_text(errors="replace").strip()
            if text.startswith("gitdir:"):
                cand = (self.root / text.split(":", 1)[1].strip()).resolve()
                back = cand / "gitdir"
                # a linked worktree's gitdir points back at this .git file; anything else is refused
                if back.is_file() and Path(os.path.realpath(back.read_text().strip())) == dotgit:
                    gitdir = cand
                    cd = gitdir / "commondir"
                    c = (gitdir / cd.read_text().strip()).resolve() if cd.exists() else gitdir
                    common = c if self._is_gitdir(c) else None
            args += ["--ro-bind", str(dotgit), str(dotgit)]
        if common is None:
            return args
        if not str(common).startswith(str(self.root) + os.sep):
            args += ["--bind", str(common), str(common)]
            wt = common / "worktrees"
            if wt.is_dir():  # other worktrees' metadata stays read-only
                args += ["--ro-bind", str(wt), str(wt)]
        if gitdir is not None and gitdir != common:
            args += ["--bind", str(gitdir), str(gitdir)]
            cw = gitdir / "config.worktree"
            if cw.exists():
                args += ["--ro-bind", str(cw), str(cw)]
        alt = common / "objects" / "info" / "alternates"
        if alt.exists():  # `git clone --shared` borrows another repository's objects: expose them read-only
            for line in alt.read_text().splitlines():
                p = Path(os.path.realpath(line.strip())) if line.strip() and not line.startswith("#") else None
                if p and p.name == "objects" and self._is_gitdir(p.parent):
                    args += ["--ro-bind", str(p), str(p)]
        hooks = common / "hooks"
        if not hooks.exists():
            hooks.mkdir(parents=True, exist_ok=True)  # an absent hooks dir would be creatable inside
        for p in (hooks, common / "config"):
            if p.exists():
                args += ["--ro-bind", str(p), str(p)]
        mods = common / "modules"
        if mods.is_dir():
            for sub in mods.rglob("config"):
                if sub.is_file():
                    args += ["--ro-bind", str(sub), str(sub)]
                    h = sub.parent / "hooks"
                    if h.is_dir():
                        args += ["--ro-bind", str(h), str(h)]
        return args

    def path_env(self) -> str:
        dirs = list(SYSTEM_PATH)
        for entry in os.environ.get("PATH", "").split(":"):
            p = Path(entry)
            if entry in dirs or not p.is_dir():
                continue
            rel = None
            try:
                rel = p.relative_to(HOME)
            except ValueError:
                pass
            if rel is not None and any(str(rel).startswith(d) for d in OVERLAY_DIRS + RO_DIRS):
                dirs.append(entry)
        return ":".join(dirs)

    def argv(self, script: str, cwd: Path, net: bool = False) -> list[str]:
        uid = os.getuid()
        a = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all"]
        if net:
            a.append("--share-net")
        # /run is masked whole: systemd-resolved's socket there resolves names even with no
        # network (a DNS exfiltration channel, found by this harness's own egress test on
        # 2026-11-08), and docker.sock / containerd.sock live there too.
        a += ["--cap-drop", "ALL", "--hostname", "h0-sandbox",
              "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
              "--tmpfs", "/run", "--tmpfs", str(HOME)]
        for d in ("/mnt", "/media"):
            if os.path.isdir(d):
                a += ["--tmpfs", d]
        if net and os.path.isdir("/run/systemd/resolve"):
            a += ["--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve"]
        nested = os.environ.get("H0_SANDBOX") == "1"  # overlayfs cannot stack on the outer sandbox's overlay
        for d in OVERLAY_DIRS:
            p = HOME / d
            if p.is_dir():
                a += ["--ro-bind", str(p), str(p)] if nested else ["--overlay-src", str(p), "--tmp-overlay", str(p)]
        for d in RO_DIRS:
            p = HOME / d
            if p.exists():
                a += ["--ro-bind", str(p), str(p)]
        for f in RO_FILES:
            p = HOME / f
            if p.exists():
                a += ["--ro-bind", str(p), str(p)]
        for p in self.ro_paths:
            if p.exists():
                a += ["--ro-bind", str(p), str(p)]
        a += ["--bind", str(self.root), str(self.root)]
        for src, dest in self.binds:  # a project's relative path dependencies, re-anchored next to the workspace
            src = os.path.expanduser(src)
            dest = os.path.normpath(os.path.expanduser(dest.replace("{root}", str(self.root))))
            if os.path.exists(src):
                a += ["--ro-bind", src, dest]
        for p in self.rw_paths:
            p.mkdir(parents=True, exist_ok=True)
            a += ["--bind", str(p), str(p)]
        a += self._git
        for p in self.protect:
            p = Path(os.path.realpath(p))
            if p.exists() and str(p).startswith(str(self.root)):
                a += ["--ro-bind", str(p), str(p)]
        for p in self._secret_masks:  # .env-shaped files in the project read as empty inside the sandbox
            a += ["--ro-bind", "/dev/null", str(p)]
        for rel in CREDENTIAL_FILES:  # tokens that live inside re-bound toolchain directories
            p = HOME / rel
            if p.is_file():
                a += ["--ro-bind", "/dev/null", str(p)]
        for p in self.mask:  # directories inside the project that must stay hidden (browser profiles, credential stores)
            if p.is_dir():
                a += ["--tmpfs", str(p)]
            elif p.exists():
                a += ["--ro-bind", "/dev/null", str(p)]
        env = {
            "PATH": self.path_env(), "HOME": str(HOME), "USER": os.environ.get("USER", "user"),
            "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "CLICOLOR": "0", "CI": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "CARGO_TERM_COLOR": "never",
            "TMPDIR": "/tmp", "XDG_CACHE_HOME": "/tmp/.cache", "npm_config_cache": "/tmp/.npm",
            "npm_config_update_notifier": "false", "PYTHONDONTWRITEBYTECODE": "1", "H0_SANDBOX": "1",
        }
        if not net:
            env["CARGO_NET_OFFLINE"] = "true"
        env.update(self.env)
        a.append("--clearenv")
        for k, v in env.items():
            a += ["--setenv", k, v]
        a += ["--chdir", str(cwd), "/usr/bin/bash", "--noprofile", "--norc", "-c", script]
        return a

    def run(self, script: str, cwd: Path | None = None, net: bool = False, timeout: float = 300) -> Result:
        cwd = cwd or self.root
        if not self.enabled:
            raise RuntimeError("sandbox disabled: refusing to run a model command unsandboxed")
        argv = self.argv(script, cwd, net)
        if self.resources:
            # A model's build must not starve the person's desktop or trip the OOM killer:
            # run it in its own systemd scope with a hard memory ceiling and low CPU weight.
            pre = []
            if shutil.which("systemd-run"):
                # --expand-environment=no: systemd-run otherwise substitutes ${...} and $VAR in
                # the command line, so bash would receive an already-mangled script
                pre = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no"]
                if self.resources.get("memory_max"):
                    pre += ["-p", f"MemoryMax={self.resources['memory_max']}", "-p", "MemorySwapMax=0"]
                if self.resources.get("cpu_weight"):
                    pre += ["-p", f"CPUWeight={self.resources['cpu_weight']}"]
                pre.append("--")
            pre += ["nice", "-n", str(self.resources.get("nice", 10))]
            argv = pre + argv
        started = time.monotonic()

        def limits() -> None:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_FSIZE, (4 << 30, 4 << 30))

        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                preexec_fn=limits, start_new_session=True)
        chunks: list[bytes] = []
        size = 0
        dropped = 0
        timed_out = False
        try:
            deadline = started + timeout
            assert proc.stdout is not None
            os.set_blocking(proc.stdout.fileno(), False)
            while True:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                try:
                    data = proc.stdout.read(65536)
                except BlockingIOError:
                    data = None
                if data:
                    if size < OUTPUT_CAP:
                        keep = data[: OUTPUT_CAP - size]
                        chunks.append(keep)
                        dropped += len(data) - len(keep)
                    else:
                        dropped += len(data)
                    size += len(data)
                    continue
                if proc.poll() is not None and data in (b"", None):
                    rest = proc.stdout.read() or b""
                    chunks.append(rest[: max(0, OUTPUT_CAP - size)])
                    break
                time.sleep(0.02)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
        out = b"".join(chunks).decode("utf-8", errors="replace")
        ms = int((time.monotonic() - started) * 1000)
        code = None if timed_out else proc.returncode
        planted = self._restore_sentinels() + ([] if self.allow_nested_git else self._remove_nested_git())
        return Result(ok=(code == 0), exit=code, output=out, ms=ms, timed_out=timed_out, truncated_bytes=dropped,
                      integrity=planted)
