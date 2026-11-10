"""Tool behaviour that saves a turn: argument checks, missing-file hints, edit near misses,
`~` meaning the sandbox's home, and transcript repair after an interrupt."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h0.loop import INTERRUPTED, repair
from h0.policy import Policy
from h0.sandbox import Sandbox
from h0.tools import Toolbox


class ToolRecovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h0-tools-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".config/hypr").mkdir(parents=True)
        (self.root / ".config/hypr/looknfeel.lua").write_text("gaps = 5   \nborder = 2\n\nanim = true\n")
        self.pol = Policy(root=self.root, headless=True)
        self.tb = Toolbox(self.root, self.pol, Sandbox(root=self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_required_argument_is_named(self):
        r = self.tb.call("read", {})
        self.assertFalse(r.ok)
        self.assertIn("missing required argument(s): path", r.text)

    def test_cut_off_call_says_so(self):
        r = self.tb.call("write", {"_unparsed": '{"path": "a.txt", "content": "half'})
        self.assertFalse(r.ok)
        self.assertIn("cut off", r.text)
        self.assertFalse((self.root / "a.txt").exists())

    def test_wrong_type_refused_and_quoted_integer_accepted(self):
        self.assertIn("should be string", self.tb.call("read", {"path": 7}).text)
        r = self.tb.call("read", {"path": ".config/hypr/looknfeel.lua", "offset": "2"})
        self.assertTrue(r.ok, r.text)
        self.assertTrue(r.text.startswith("2\t"))
        self.assertIn("should be boolean", self.tb.call("edit", {"path": "x", "old": "a", "new": "b", "replace_all": "yes"}).text)

    def test_absolute_path_meant_relative_is_suggested(self):
        r = self.tb.call("read", {"path": "/.config/hypr/looknfeel.lua"})
        self.assertFalse(r.ok)
        self.assertIn("Did you mean: .config/hypr/looknfeel.lua", r.text)

    def test_right_name_wrong_directory_is_suggested(self):
        r = self.tb.call("edit", {"path": "config/looknfeel.lua", "old": "a", "new": "b"})
        self.assertIn(".config/hypr/looknfeel.lua", r.text)
        self.assertIn("use write", r.text)

    def test_hint_never_names_a_secret_file(self):
        (self.root / "deep").mkdir()
        (self.root / "deep/.env").write_text("TOKEN=x\n")
        r = self.tb.call("read", {"path": ".env"})
        self.assertNotIn("deep/.env", r.text)

    def test_edit_ignores_trailing_whitespace_when_unique(self):
        r = self.tb.call("edit", {"path": ".config/hypr/looknfeel.lua", "old": "gaps = 5\nborder = 2\n", "new": "gaps = 8\nborder = 2\n"})
        self.assertTrue(r.ok, r.text)
        self.assertIn("ignoring trailing whitespace", r.text)
        self.assertEqual((self.root / ".config/hypr/looknfeel.lua").read_text(), "gaps = 8\nborder = 2\n\nanim = true\n")

    def test_edit_does_not_loosen_indentation(self):
        f = self.root / "x.py"
        f.write_text("def f():\n    return 1\n")
        r = self.tb.call("edit", {"path": "x.py", "old": "def f():\n  return 1", "new": "def f():\n  return 2"})
        self.assertFalse(r.ok)
        self.assertIn("closest text (lines 1-2", r.text)
        self.assertIn("1\tdef f():", r.text)
        self.assertEqual(f.read_text(), "def f():\n    return 1\n")

    def test_edit_ambiguous_loose_match_refused(self):
        f = self.root / "y.txt"
        f.write_text("a  \nb\na\nb\n")
        r = self.tb.call("edit", {"path": "y.txt", "old": "a \nb", "new": "c"})
        self.assertFalse(r.ok)
        self.assertEqual(f.read_text(), "a  \nb\na\nb\n")

    def test_edit_with_read_line_numbers_is_explained(self):
        r = self.tb.call("edit", {"path": ".config/hypr/looknfeel.lua", "old": "1\tgaps = 5\n2\tborder = 2", "new": "x"})
        self.assertIn("line number and a tab", r.text)


class SeenThroughBash(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h0-seen-"))
        (self.root / "a.txt").write_text("one\n")
        (self.root / "b.txt").write_text("two\n")
        self.tb = Toolbox(self.root, Policy(root=self.root, headless=True), Sandbox(root=self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cat_counts_as_reading_but_grep_does_not(self):
        self.assertFalse(self.tb.call("write", {"path": "a.txt", "content": "x"}).ok)
        self.assertTrue(self.tb.call("bash", {"command": "cat a.txt"}).ok)
        self.assertTrue(self.tb.call("write", {"path": "a.txt", "content": "x"}).ok)
        self.tb.call("bash", {"command": "grep two b.txt"})
        self.assertFalse(self.tb.call("write", {"path": "b.txt", "content": "x"}).ok)

    def test_a_secret_file_never_becomes_seen(self):
        (self.root / ".env").write_text("K=v\n")
        self.tb.call("bash", {"command": "cat .env"})
        self.assertNotIn((self.root / ".env").resolve(), self.tb.seen)


class SandboxHome(unittest.TestCase):
    """When a run gives the sandbox its own HOME, `~` in the file tools means that HOME too."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h0-home-"))
        (self.root / ".config").mkdir()
        (self.root / ".config/a.conf").write_text("x\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_tilde_is_the_sandbox_home(self):
        pol = Policy(root=self.root, headless=True, home=self.root)
        tb = Toolbox(self.root, pol, Sandbox(root=self.root))
        self.assertTrue(tb.call("read", {"path": "~/.config/a.conf"}).ok)
        r = tb.call("edit", {"path": "~/.config/a.conf", "old": "x", "new": "y"})
        self.assertTrue(r.ok, r.text)

    def test_tilde_cannot_climb_out_of_the_sandbox_home(self):
        pol = Policy(root=self.root, headless=True, home=self.root)
        tb = Toolbox(self.root, pol, Sandbox(root=self.root))
        r = tb.call("write", {"path": "~/../escaped.txt", "content": "x"})
        self.assertFalse(r.ok)
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        r = tb.call("read", {"path": "~/../../../" + str(Path.home().relative_to("/")) + "/.ssh/id_ed25519"})
        self.assertFalse(r.ok)

    def test_root_dot_directory_slip_is_read_in_the_session_home(self):
        tb = Toolbox(self.root, Policy(root=self.root, headless=True, home=self.root), Sandbox(root=self.root))
        r = tb.call("read", {"path": "/.config/a.conf"})
        self.assertTrue(r.ok, r.text)
        self.assertIn("was read as .config/a.conf", r.text)
        w = tb.call("write", {"path": "/.config/new.conf", "content": "z\n"})
        self.assertTrue(w.ok, w.text)
        self.assertEqual((self.root / ".config/new.conf").read_text(), "z\n")

    def test_real_home_path_means_the_session_home(self):
        tb = Toolbox(self.root, Policy(root=self.root, headless=True, home=self.root), Sandbox(root=self.root))
        r = tb.call("read", {"path": str(Path.home() / ".config/a.conf")})
        self.assertTrue(r.ok, r.text)
        self.assertIn("x", r.text)

    def test_slips_are_not_read_without_a_moved_home(self):
        tb = Toolbox(self.root, Policy(root=self.root, headless=True), Sandbox(root=self.root))
        self.assertFalse(tb.call("read", {"path": "/.config/a.conf"}).ok)
        self.assertFalse(tb.call("read", {"path": str(Path.home() / ".config/a.conf")}).ok)

    def test_slip_reading_cannot_leave_the_project(self):
        tb = Toolbox(self.root, Policy(root=self.root, headless=True, home=self.root), Sandbox(root=self.root))
        w = tb.call("write", {"path": "/.config/../../escaped.txt", "content": "x"})
        self.assertFalse(w.ok)
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        r = tb.call("read", {"path": str(Path.home() / ".ssh/id_ed25519")})
        self.assertFalse(r.ok)

    def test_tilde_user_form_is_not_rewritten(self):
        # only `~` and `~/…` are the session's home; `~root` keeps its ordinary meaning
        pol = Policy(root=self.root, headless=True, home=self.root)
        self.assertEqual(str(pol.resolve("~root")), "/root")

    def test_without_a_sandbox_home_tilde_is_the_real_home(self):
        pol = Policy(root=self.root, headless=True)
        self.assertEqual(pol.resolve("~/.ssh"), Path(Path.home(), ".ssh").resolve())
        self.assertFalse(pol.check_read(pol.resolve("~/.ssh/id_ed25519")).allowed)


class EnvironmentLine(unittest.TestCase):
    def test_home_is_named_only_when_the_run_moves_it(self):
        from h0 import context
        root = Path("/w/proj")
        self.assertNotIn("home directory", context.environment(root))
        self.assertIn("(~) is the project root itself", context.environment(root, home=root))
        self.assertIn("(~) is /w/proj/home", context.environment(root, home=root / "home"))


class TranscriptRepair(unittest.TestCase):
    def test_dangling_calls_get_results(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}},
                                                  {"type": "tool_use", "id": "b", "name": "read", "input": {}}]}]
        self.assertEqual(repair(msgs), 2)
        self.assertEqual([b["tool_use_id"] for b in msgs[2]["content"]], ["a", "b"])
        self.assertEqual(msgs[2]["content"][0]["content"], INTERRUPTED)

    def test_partial_results_completed_and_text_kept_last(self):
        msgs = [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}},
                                                  {"type": "tool_use", "id": "b", "name": "read", "input": {}}]},
                {"role": "user", "content": "next question"}]
        self.assertEqual(repair(msgs), 2)
        self.assertEqual(msgs[1]["content"][-1], {"type": "text", "text": "next question"})
        msgs2 = [{"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}},
                                                   {"type": "tool_use", "id": "b", "name": "read", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}]
        self.assertEqual(repair(msgs2), 1)
        self.assertEqual({b["tool_use_id"] for b in msgs2[1]["content"]}, {"a", "b"})

    def test_complete_transcript_untouched(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        before = repr(msgs)
        self.assertEqual(repair(msgs), 0)
        self.assertEqual(repr(msgs), before)


if __name__ == "__main__":
    unittest.main()
