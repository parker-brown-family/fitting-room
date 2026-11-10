"""Tree undo: whatever changed the writable roots during a request, undo puts them back, and never
writes outside them. The Omarchy release promise is that a change can be undone; 311 of 713 Omarchy
session traces wrote through bash, which the file tools' own record never saw."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from h0 import config
from h0.loop import Agent
from h0.policy import Policy
from h0.providers.fake import Fake
from h0.sandbox import Sandbox
from h0.tools import Toolbox
from h0.trace import Trace
from h0.undo import SMALL, TreeUndo
from tests.test_loop import QuietUI, tool


def snapshot(*roots: Path) -> dict:
    out = {}
    for r in roots:
        for d, dn, fn in os.walk(r):
            out[d] = "dir"
            for f in fn:
                p = os.path.join(d, f)
                out[p] = os.readlink(p) if os.path.islink(p) else (Path(p).read_bytes(), os.stat(p).st_mode)
            for f in dn:
                p = os.path.join(d, f)
                if os.path.islink(p):
                    out[p] = os.readlink(p)
    return out


class TreeUndoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h0-undo-"))
        self.root = self.tmp / "hypr"
        self.other = self.tmp / "omarchy"           # a second writable root, like ~/.config/omarchy
        self.outside = self.tmp / "outside"         # never writable
        for d in (self.root, self.other, self.outside):
            d.mkdir()
        (self.root / "bindings.lua").write_text("-- bindings\no.bind('SUPER', 'Return', 'exec', 'terminal')\n")
        (self.root / "autostart.lua").write_text("-- autostart\n")
        (self.other / "shell.json").write_text('{\n  "bar": {}\n}\n')
        os.chmod(self.other / "shell.json", 0o600)
        (self.other / "themes").mkdir()
        (self.outside / "secret.txt").write_text("keep me\n")
        self.store = self.tmp / "store"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, script, tree=True):
        s = config.Settings(model="fake", headless=True, mode="yolo")
        pol = Policy(root=self.root, write_roots=[self.other], read_roots=[self.other], headless=True, mode="yolo")
        sb = Sandbox(root=self.root, rw_paths=[self.other])
        tb = Toolbox(self.root, pol, sb)
        if tree:
            tb.tree = TreeUndo([self.root, self.other], self.store)
        return Agent(settings=s, provider=Fake(script), toolbox=tb, policy=pol, trace=Trace(None), ui=QuietUI(),
                     system="sys")

    def test_changes_made_through_bash_are_put_back_byte_for_byte(self):
        before = snapshot(self.root, self.other)
        cmd = (f"echo \"o.launch_on_start('nm-applet')\" >> autostart.lua; "
               f"sed -i 's/Return/T/' bindings.lua; cp bindings.lua bindings.lua.bak; "
               f"mkdir -p {self.other}/themes/probe/sub && echo x > {self.other}/themes/probe/sub/colors.toml; "
               f"chmod 644 {self.other}/shell.json; rm autostart.lua")
        a = self.agent([[tool("bash", command=cmd)]])
        a.run("change things through bash")
        self.assertNotEqual(snapshot(self.root, self.other), before)
        undone = a.undo()
        self.assertTrue(undone)
        self.assertEqual(snapshot(self.root, self.other), before)

    def test_a_killed_session_is_undone_from_the_store_alone(self):
        before = snapshot(self.root, self.other)
        a = self.agent([[tool("bash", command=f"echo broken > {self.other}/shell.json")]])
        a.run("break the bar")
        del a                                           # the process is gone; only the store remains
        r = TreeUndo.open(self.store).undo()
        self.assertEqual(r["failed"], [])
        self.assertEqual(snapshot(self.root, self.other), before)

    def test_undo_skips_requests_that_changed_nothing(self):
        before = snapshot(self.root, self.other)
        a = self.agent([[tool("bash", command="echo more >> autostart.lua")], [{"type": "text", "text": "ok"}],
                        [tool("bash", command="cat autostart.lua")]])
        a.run("change")
        a.run("just look")
        self.assertTrue(a.undo())
        self.assertEqual(snapshot(self.root, self.other), before)
        self.assertEqual(a.undo(), [])

    def test_a_large_file_that_changed_is_named_not_silently_skipped(self):
        big = self.other / "wallpaper.png"
        big.write_bytes(b"\0" * (SMALL + 1))
        a = self.agent([[tool("bash", command=f"printf x >> {big}")]])
        a.run("touch the wallpaper")
        undone = a.undo()
        self.assertTrue(any("wallpaper.png (NOT put back)" in u for u in undone), undone)

    def test_the_note_to_the_model_only_says_bash_was_not_undone_when_it_was_not(self):
        a = self.agent([[tool("bash", command="echo x >> autostart.lua")]])
        a.run("x")
        a.undo()
        self.assertNotIn("bash were not undone", a.pending_notes[-1])
        b = self.agent([[tool("read", path="autostart.lua")],
                        [tool("edit", path="autostart.lua", old="-- autostart\n", new="-- autostart!\n")]], tree=False)
        b.run("x")
        b.undo()
        self.assertIn("bash were not undone", b.pending_notes[-1])

    # -- attempts to make a restore write where it should not ---------------------
    def test_a_tampered_index_cannot_write_outside_the_roots(self):
        t = TreeUndo([self.root, self.other], self.store)
        t.checkpoint()
        cp = sorted(self.store.glob("cp-*.json"))[-1]
        idx = json.loads(cp.read_text())
        victim = str(self.outside / "secret.txt")
        idx["files"][victim] = idx["files"][str(self.root / "bindings.lua")]
        cp.write_text(json.dumps(idx))
        r = t.undo()
        self.assertIn(victim, r["failed"])
        self.assertEqual((self.outside / "secret.txt").read_text(), "keep me\n")

    def test_a_path_inside_the_roots_that_resolves_outside_them_is_refused(self):
        # Omarchy keeps symlinks under its config (current/theme points at a theme folder), so a path
        # can read as inside a root and land outside it. A real index never lists files beneath a
        # link; a tampered one could, and only the realpath check stands between it and the target.
        os.symlink(self.outside, self.other / "current")
        t = TreeUndo([self.root, self.other], self.store)
        t.checkpoint()
        cp = sorted(self.store.glob("cp-*.json"))[-1]
        idx = json.loads(cp.read_text())
        through = str(self.other / "current" / "secret.txt")
        idx["files"][through] = idx["files"][str(self.root / "bindings.lua")]
        cp.write_text(json.dumps(idx))
        r = t.undo()
        self.assertIn(through, r["failed"])
        self.assertEqual((self.outside / "secret.txt").read_text(), "keep me\n")

    def test_a_directory_swapped_for_a_symlink_is_not_followed_on_restore(self):
        # removal runs before restore, so the link is gone before anything is written beneath it
        (self.other / "themes" / "a.toml").write_text("accent\n")
        before_outside = snapshot(self.outside)
        a = self.agent([[tool("bash", command=f"rm -rf {self.other}/themes && ln -s {self.outside} {self.other}/themes")]])
        a.run("swap a directory for a link")
        r = a.toolbox.tree.undo()
        self.assertEqual(snapshot(self.outside), before_outside)
        self.assertFalse((self.outside / "a.toml").exists())
        self.assertFalse(os.path.islink(self.other / "themes"), r)

    def test_a_new_symlink_out_of_the_roots_is_removed_and_its_target_kept(self):
        a = self.agent([[tool("bash", command=f"ln -s {self.outside}/secret.txt link.txt")]])
        a.run("link out")
        a.undo()
        self.assertFalse(os.path.lexists(self.root / "link.txt"))
        self.assertEqual((self.outside / "secret.txt").read_text(), "keep me\n")

    def test_the_store_is_private(self):
        TreeUndo([self.root], self.store, self.tmp / "blobs")
        self.assertEqual(os.stat(self.store).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.tmp / "blobs").st_mode & 0o777, 0o700)

    # -- what it keeps on disk -------------------------------------------------------
    def test_sessions_share_one_copy_of_unchanged_files(self):
        blobs = self.tmp / "blobs"
        TreeUndo([self.root, self.other], self.tmp / "s1.undo", blobs).checkpoint()
        n = len(list(blobs.iterdir()))
        TreeUndo([self.root, self.other], self.tmp / "s2.undo", blobs).checkpoint()
        self.assertEqual(len(list(blobs.iterdir())), n)

    def test_only_the_last_checkpoints_are_kept_and_undo_still_works(self):
        from h0.undo import KEEP
        t = TreeUndo([self.root], self.store)
        for i in range(KEEP + 5):
            (self.root / "autostart.lua").write_text(f"-- autostart {i}\n")
            t.checkpoint()
        self.assertEqual(len(list(self.store.glob("cp-*.json"))), KEEP)
        (self.root / "autostart.lua").write_text("-- broken\n")
        t.undo()
        self.assertEqual((self.root / "autostart.lua").read_text(), f"-- autostart {KEEP + 4}\n")

    def test_prune_drops_old_sessions_and_only_the_blobs_nobody_uses(self):
        from h0.undo import prune
        sessions, blobs = self.tmp / "sessions", self.tmp / "blobs"
        TreeUndo([self.root], sessions / "old.undo", blobs).checkpoint()
        (self.other / "only-new.txt").write_text("new\n")
        TreeUndo([self.other], sessions / "new.undo", blobs).checkpoint()
        week3 = 21 * 86400
        for p in [sessions / "old.undo", *blobs.iterdir()]:
            os.utime(p, (p.stat().st_atime - week3, p.stat().st_mtime - week3))
        removed = prune(sessions, blobs)
        self.assertFalse((sessions / "old.undo").exists())
        self.assertTrue((sessions / "new.undo").exists())
        self.assertGreater(removed, 0)
        r = TreeUndo.open(sessions / "new.undo")
        (self.other / "only-new.txt").write_text("changed\n")
        r.undo()
        self.assertEqual((self.other / "only-new.txt").read_text(), "new\n")

    def test_prune_removes_nothing_when_an_index_cannot_be_read(self):
        from h0.undo import prune
        sessions, blobs = self.tmp / "sessions", self.tmp / "blobs"
        TreeUndo([self.root], sessions / "a.undo", blobs).checkpoint()
        (sessions / "a.undo" / "cp-00009.json").write_text("{not json")
        for b in blobs.iterdir():
            os.utime(b, (0, 0))
        self.assertEqual(prune(sessions, blobs), 0)

    # -- inspect: what changed, read from the store rather than from the model ------------
    def test_changes_shows_what_bash_changed_as_a_diff(self):
        cmd = (f"sed -i 's/Return/T/' bindings.lua; echo new > created.lua; rm autostart.lua; "
               f"printf x > {self.other}/shell.json")
        a = self.agent([[tool("bash", command=cmd)]])
        a.run("change things")
        ch = a.toolbox.tree.changes("last")
        kinds = {Path(f["path"]).name: f["kind"] for f in ch["files"]}
        self.assertEqual(kinds, {"bindings.lua": "changed", "created.lua": "created", "autostart.lua": "deleted",
                                 "shell.json": "changed"})
        diff = next(f["diff"] for f in ch["files"] if f["path"].endswith("bindings.lua"))
        self.assertIn("-o.bind('SUPER', 'Return'", diff)
        self.assertIn("+o.bind('SUPER', 'T'", diff)
        self.assertIn("+new", next(f["diff"] for f in ch["files"] if f["path"].endswith("created.lua")))

    def test_a_backup_copy_is_named_as_a_copy_not_printed(self):
        a = self.agent([[tool("bash", command="cp bindings.lua bindings.lua.bak.1")]])
        a.run("back up")
        f = a.toolbox.tree.changes("last")["files"]
        self.assertEqual(len(f), 1)
        self.assertEqual((Path(f[0]["path"]).name, f[0]["kind"], f[0]["diff"]), ("bindings.lua.bak.1", "created", None))
        self.assertEqual(Path(f[0]["copy_of"]).name, "bindings.lua")

    def test_changes_names_a_large_file_without_a_diff_and_lists_nothing_when_nothing_changed(self):
        big = self.other / "wallpaper.png"
        big.write_bytes(b"\0" * (SMALL + 1))
        a = self.agent([[tool("bash", command="cat bindings.lua")], [{"type": "text", "text": "ok"}],
                        [tool("bash", command=f"printf x >> {big}")]])
        a.run("look only")
        self.assertEqual(a.toolbox.tree.changes("last")["files"], [])
        a.run("touch the wallpaper")
        f = a.toolbox.tree.changes("last")["files"]
        self.assertEqual([(Path(x["path"]).name, x["kind"], x["diff"]) for x in f], [("wallpaper.png", "changed", None)])

    def test_changes_is_read_only(self):
        before = snapshot(self.root, self.other)
        t = TreeUndo([self.root, self.other], self.store)
        t.checkpoint()
        (self.root / "autostart.lua").write_text("-- edited\n")
        mid = snapshot(self.root, self.other)
        t.changes("last")
        t.changes("all")
        self.assertEqual(snapshot(self.root, self.other), mid)
        self.assertEqual(len(list(self.store.glob("cp-*.json"))), 1)
        self.assertNotEqual(mid, before)


if __name__ == "__main__":
    unittest.main()
