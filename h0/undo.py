"""Tree undo: put a fitting's writable roots back as they were before a request, whatever changed them.

The file tools record what they overwrite, but a model writes through bash about as often (311 of
713 Omarchy session traces, 2026-11-10), and those changes were never undone. So a fitting that
declares `undo = "tree"` gets this instead: before each request every file under its writable roots
is indexed, and files up to SMALL bytes are kept content-addressed on disk beside the session. Undo
restores changed and deleted files, removes the ones the request created, and names any file it
could not put back (a large file that changed). The store is on disk, so a session that was killed
can still be undone with `h0 undo`.

The store sits in h0's state directory, which the sandbox hides from the model. Even so, a restore
refuses any path that is not under one of the roots once symlinks are resolved, so an index that
was tampered with, or a directory swapped for a symlink during the request, cannot steer a write
outside them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

SMALL = 1 << 20          # files up to this size are kept and can be restored
MAX_FILES = 50_000       # past this the index is marked partial rather than growing without bound
KEEP = 20                # checkpoints kept per session, as the file tools' own record keeps
FRESH_S = 3600           # a blob touched this recently may belong to an index still being written


class TreeUndo:
    def __init__(self, roots: list[Path], store: Path, blobs: Path | None = None):
        """`store` holds this session's indexes; `blobs` holds file contents and may be shared by every
        session, so a 60 MB Omarchy config is kept once rather than once per session."""
        self.roots = sorted({Path(os.path.realpath(r)) for r in roots if Path(r).exists()})
        self.store = Path(store)
        self.blobs = Path(blobs) if blobs else self.store / "blobs"
        for d in (self.store, self.blobs):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        meta = self.store / "roots.json"
        if not meta.exists():
            meta.write_text(json.dumps({"roots": [str(r) for r in self.roots], "blobs": str(self.blobs)}))
        self._cache: dict[str, tuple] = {}     # path -> (size, mtime_ns, ino, sha) for files already stored

    @classmethod
    def open(cls, store: Path) -> "TreeUndo":
        meta = json.loads((Path(store) / "roots.json").read_text())
        return cls([Path(p) for p in meta["roots"]], store, Path(meta["blobs"]))

    # -- indexing ----------------------------------------------------------------
    def _blob(self, p: str, st: os.stat_result) -> str:
        key = (st.st_size, st.st_mtime_ns, st.st_ino)
        hit = self._cache.get(p)
        if hit and hit[:3] == key:
            return hit[3]
        data = Path(p).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        b = self.blobs / sha
        if b.exists():
            os.utime(b)         # referenced again: keep it out of a concurrent prune
        else:
            fd, tmp = tempfile.mkstemp(dir=b.parent)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, b)
        self._cache[p] = (*key, sha)
        return sha

    def index(self) -> dict:
        files: dict[str, dict] = {}
        dirs: list[str] = []
        partial = False
        for root in self.roots:
            for d, dnames, fnames in os.walk(root, followlinks=False):
                dirs.append(d)
                for name in dnames + fnames:
                    p = os.path.join(d, name)
                    if len(files) >= MAX_FILES:
                        partial = True
                        break
                    try:
                        st = os.lstat(p)
                    except OSError:
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        files[p] = {"t": "link", "to": os.readlink(p)}
                    elif stat.S_ISREG(st.st_mode):
                        mode = stat.S_IMODE(st.st_mode)
                        if st.st_size <= SMALL:
                            try:
                                files[p] = {"t": "file", "sha": self._blob(p, st), "mode": mode}
                            except OSError:
                                files[p] = {"t": "big", "size": st.st_size, "mtime": st.st_mtime_ns, "mode": mode}
                        else:
                            files[p] = {"t": "big", "size": st.st_size, "mtime": st.st_mtime_ns, "mode": mode}
        return {"files": files, "dirs": dirs, "partial": partial}

    def checkpoint(self) -> None:
        have = self._checkpoint_files()
        n = int(have[-1].stem[3:]) + 1 if have else 0
        tmp = self.store / f".cp-{n:05d}.json"
        tmp.write_text(json.dumps(self.index()))
        os.replace(tmp, self.store / f"cp-{n:05d}.json")
        for old in self._checkpoint_files()[:-KEEP]:
            old.unlink()

    def _checkpoint_files(self) -> list[Path]:
        return sorted(self.store.glob("cp-*.json"))

    # -- comparing and restoring -------------------------------------------------
    def _same(self, p: str, want: dict) -> bool:
        try:
            st = os.lstat(p)
        except OSError:
            return False
        if want["t"] == "link":
            return stat.S_ISLNK(st.st_mode) and os.readlink(p) == want["to"]
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != want["mode"]:
            return False
        if want["t"] == "big":
            return st.st_size == want["size"] and st.st_mtime_ns == want["mtime"]
        return self._blob(p, st) == want["sha"] if st.st_size <= SMALL else False

    def plan(self, idx: dict) -> dict:
        now = self.index()
        before, after = idx["files"], now["files"]
        restore = sorted(p for p, e in before.items() if not self._same(p, e))
        remove = sorted(p for p in after if p not in before)
        dirs = sorted((d for d in now["dirs"] if d not in set(idx["dirs"])), key=len, reverse=True)
        return {"restore": restore, "remove": remove, "dirs": dirs, "partial": idx.get("partial") or now["partial"]}

    def _inside(self, p: str) -> bool:
        real = os.path.realpath(os.path.dirname(p))
        return any(real == str(r) or real.startswith(str(r) + os.sep) for r in self.roots)

    def apply(self, idx: dict, plan: dict) -> tuple[list[str], list[str], list[str]]:
        """Returns (put back, removed because the request created them, could not put back)."""
        done, removed, failed = [], [], []
        for p in plan["remove"]:
            if not self._inside(p):
                failed.append(p)
                continue
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    continue            # a new directory goes with the empty-directory sweep below
                os.unlink(p)
                removed.append(p)
            except OSError:
                failed.append(p)
        for d in plan["dirs"]:
            if self._inside(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass                # not empty: something in it was not ours to remove
        for p in plan["restore"]:
            e = idx["files"][p]
            if e["t"] == "big" or not self._inside(p):
                failed.append(p)
                continue
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                if not self._inside(p):          # a parent swapped for a symlink since the check above
                    failed.append(p)
                    continue
                if os.path.lexists(p) and (os.path.islink(p) or not os.path.isfile(p)):
                    os.unlink(p) if not os.path.isdir(p) else os.rmdir(p)
                if e["t"] == "link":
                    os.symlink(e["to"], p)
                else:
                    data = (self.blobs / e["sha"]).read_bytes()
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".h0-undo-")
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    os.chmod(tmp, e["mode"])
                    os.replace(tmp, p)
                    self._cache.pop(p, None)
                done.append(p)
            except OSError:
                failed.append(p)
        return done, removed, failed

    def changes(self, since: str = "last") -> dict | None:
        """What changed on disk since the last request began ("last") or since the oldest kept
        checkpoint ("all"), whatever made the change: a diff for each text file up to SMALL, and
        the name for anything else. Read from the store, so it shows what happened rather than
        what the model said happened. None when there is no checkpoint."""
        cps = self._checkpoint_files()
        if not cps:
            return None
        idx = json.loads((cps[-1] if since == "last" else cps[0]).read_text())
        plan = self.plan(idx)
        files = []
        for p in plan["restore"]:
            e = idx["files"][p]
            if not os.path.lexists(p):
                files.append({"path": p, "kind": "deleted", "diff": None})
                continue
            files.append({"path": p, "kind": "changed", "diff": self._diff(p, e)})
        by_sha = {e["sha"]: q for q, e in idx["files"].items() if e["t"] == "file"}
        for p in plan["remove"]:
            entry = {"path": p, "kind": "created", "diff": None}
            try:
                if os.path.isfile(p) and not os.path.islink(p) and os.path.getsize(p) <= SMALL:
                    sha = hashlib.sha256(Path(p).read_bytes()).hexdigest()
                    if sha in by_sha:     # a backup: say what it copies rather than print it all
                        entry["copy_of"] = by_sha[sha]
            except OSError:
                pass
            if "copy_of" not in entry:
                entry["diff"] = self._diff(p, None)
            files.append(entry)
        return {"files": sorted(files, key=lambda f: f["path"]), "partial": plan["partial"], "checkpoint": cps[-1 if since == "last" else 0].stem}

    def _diff(self, p: str, before: dict | None) -> str | None:
        """A unified diff, or None when either side is not small text (a wallpaper, a link, a binary)."""
        import difflib
        if before is not None and before["t"] != "file":
            return None
        try:
            if os.path.islink(p) or os.path.getsize(p) > SMALL:
                return None
            now = Path(p).read_bytes()
            old = (self.blobs / before["sha"]).read_bytes() if before else b""
        except OSError:
            return None
        if b"\0" in now[:8192] or b"\0" in old[:8192]:
            return None
        a = old.decode("utf-8", errors="replace").splitlines(keepends=True)
        b = now.decode("utf-8", errors="replace").splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, "before", "now", n=2))

    def undo(self) -> dict | None:
        """Undo the most recent request that changed anything. None when there is nothing to undo."""
        for cp in reversed(self._checkpoint_files()):
            idx = json.loads(cp.read_text())
            plan = self.plan(idx)
            cp.unlink()
            if plan["restore"] or plan["remove"] or plan["dirs"]:
                done, removed, failed = self.apply(idx, plan)
                return {"restored": done, "removed": removed, "failed": failed, "partial": plan["partial"]}
        return None


def prune(sessions: Path, blobs: Path, days: float = 14) -> int:
    """Drop session undo stores older than `days`, then every blob no remaining index refers to.
    Returns the number of blobs removed."""
    sessions, blobs = Path(sessions), Path(blobs)
    if not blobs.is_dir():
        return 0
    cutoff = time.time() - days * 86400
    stores = list(sessions.glob("*.undo")) if sessions.is_dir() else []
    expired = [st for st in stores if st.stat().st_mtime < cutoff]
    if not expired:
        return 0                # reading every index is only worth it when a session has gone
    for st in expired:
        shutil.rmtree(st, ignore_errors=True)
    used: set[str] = set()
    for cp in sessions.glob("*.undo/cp-*.json"):
        try:
            used |= {e["sha"] for e in json.loads(cp.read_text())["files"].values() if e["t"] == "file"}
        except (OSError, ValueError, KeyError):
            return 0            # an index we cannot read might refer to anything: remove nothing
    removed = 0
    fresh = time.time() - FRESH_S
    for b in blobs.iterdir():
        if b.name not in used and b.stat().st_mtime < fresh:
            b.unlink(missing_ok=True)
            removed += 1
    return removed
