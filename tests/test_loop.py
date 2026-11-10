"""Loop behaviour with the scripted provider: tool dispatch, triggers, verify-on-finish."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h0 import config
from h0.fitting import Fitting
from h0.loop import Agent
from h0.policy import Policy
from h0.providers.fake import Fake
from h0.sandbox import Sandbox
from h0.tools import Toolbox
from h0.trace import Trace


class QuietUI:
    def text(self, s): pass
    def thinking(self, s): pass
    def tool_start(self, n, a): pass
    def tool_end(self, n, r, ms): pass
    def note(self, s): pass
    def turn_end(self, t): pass
    def ask(self, w, d): return False


def tool(name, **inp):
    return {"type": "tool_use", "id": f"t_{name}_{len(inp)}_{abs(hash(str(inp))) % 10000}", "name": name, "input": inp}


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h0-loop-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.txt").write_text("hello\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def agent(self, script, fitting=None):
        s = config.Settings(model="fake", headless=True)
        pol = Policy(root=self.root, headless=True)
        tb = Toolbox(self.root, pol, Sandbox(root=self.root), checks=(fitting.checks if fitting else {}))
        tr = Trace(None)
        return Agent(settings=s, provider=Fake(script), toolbox=tb, policy=pol, trace=tr, ui=QuietUI(), system="sys", fitting=fitting)

    def test_a_turn_served_by_another_model_stops_the_run(self):
        # a score served by another model measures nothing, so it must never be counted
        a = self.agent([[{"type": "text", "text": "hi"}]])
        a.provider.served_model = "claude-other-1"
        out = a.run("hello")
        self.assertEqual(out["stop"], "error")
        a2 = self.agent([[{"type": "text", "text": "hi"}]])
        a2.provider.served_model = a2.settings.model
        self.assertEqual(a2.run("hello")["stop"], "end_turn")

    def test_interrupted_transcript_is_repaired_before_the_next_request(self):
        # Ctrl-C between a tool call and its result used to leave a transcript the API refuses
        a = self.agent([[{"type": "text", "text": "ok"}]])
        a.messages = [{"role": "user", "content": "go"},
                      {"role": "assistant", "content": [tool("bash", command="sleep 100")]}]
        self.assertEqual(a.run("carry on")["stop"], "end_turn")
        sent = a.provider.calls[0]["messages"]
        self.assertEqual(sent[2]["content"][0]["type"], "tool_result")
        self.assertTrue(sent[2]["content"][0]["is_error"])

    def test_undo_restores_the_last_request_and_tells_the_model(self):
        a = self.agent([[tool("edit", path="a.txt", old="hello", new="bye"), tool("write", path="new.txt", content="x")],
                        [{"type": "text", "text": "done"}],
                        [{"type": "text", "text": "noted"}]])
        a.run("change things")
        self.assertEqual((self.root / "a.txt").read_text(), "bye\n")
        self.assertEqual(sorted(a.undo()), ["a.txt", "new.txt"])
        self.assertEqual((self.root / "a.txt").read_text(), "hello\n")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(a.undo(), [])
        a.run("what now?")
        self.assertIn("undid the file changes", a.messages[-2]["content"])

    def test_turn_budget_is_announced_twice(self):
        a = self.agent([[tool("read", path="a.txt")]] * 12)
        a.settings.max_turns = 12
        out = a.run("loop forever")
        self.assertEqual(out["stop"], "max_turns")
        notes = [(i, b["content"]) for i, m in enumerate(a.messages) if m["role"] == "user" and isinstance(m["content"], list)
                 for b in m["content"] if "turn" in b.get("content", "").split("[harness note]")[-1]]
        self.assertEqual(len(notes), 2)
        self.assertIn("8 of 12 turns used, 4 left", notes[0][1])
        self.assertIn("One turn left", notes[1][1])

    def test_short_sessions_hear_nothing_about_budget(self):
        a = self.agent([[tool("read", path="a.txt")], [{"type": "text", "text": "done"}]])
        a.run("go")
        self.assertNotIn("[harness note]", a.messages[2]["content"][0]["content"])

    def test_tool_round_trip(self):
        a = self.agent([[tool("read", path="a.txt")], [{"type": "text", "text": "it says hello"}]])
        out = a.run("what does a.txt say?")
        self.assertEqual(out["stop"], "end_turn")
        self.assertEqual(out["final"], "it says hello")
        results = a.messages[2]["content"]
        self.assertEqual(results[0]["type"], "tool_result")
        self.assertIn("hello", results[0]["content"])

    def test_trigger_block_and_inject(self):
        fit = Fitting(name="t", root=self.root, triggers=[
            {"on": "before:bash", "match": r"git\s+push", "action": "block", "note": "never push from h0"},
            {"on": "after:edit", "match": r"\.txt$", "action": "inject", "note": "text files are user-facing"},
        ])
        a = self.agent([[tool("bash", command="git push origin main")],
                        [tool("edit", path="a.txt", old="hello", new="hi")],
                        [{"type": "text", "text": "ok"}]], fit)
        a.run("go")
        push_result = a.messages[2]["content"][0]
        self.assertTrue(push_result["is_error"])
        self.assertIn("never push", push_result["content"])
        edit_result = a.messages[4]["content"][0]
        self.assertIn("text files are user-facing", edit_result["content"])

    def test_verify_on_finish_feeds_failure_back(self):
        fit = Fitting(name="t", root=self.root, checks={"ok": {"run": "grep -q fixed a.txt"}}, verify="ok")
        a = self.agent([[{"type": "text", "text": "done"}],
                        [tool("edit", path="a.txt", old="hello", new="fixed")],
                        [{"type": "text", "text": "really done"}]], fit)
        out = a.run("fix it")
        self.assertEqual(out["stop"], "end_turn")
        self.assertIn("check must pass", a.messages[2]["content"])
        self.assertEqual(out["final"], "really done")

    def test_finish_review_asks_once_then_lets_the_session_end(self):
        a = self.agent([[{"type": "text", "text": "done"}], [{"type": "text", "text": "all met"}]])
        a.settings.finish_review = True
        out = a.run("fix it")
        self.assertEqual(out["stop"], "end_turn")
        self.assertEqual(out["final"], "all met")
        self.assertIn("marked met or not met", a.messages[2]["content"])
        self.assertEqual(sum("marked met or not met" in str(m["content"]) for m in a.messages), 1)

    def test_no_finish_review_by_default(self):
        out = self.agent([[{"type": "text", "text": "done"}]]).run("fix it")
        self.assertEqual(out["final"], "done")

    def test_empty_reply_is_nudged_once(self):
        a = self.agent([[{"type": "thinking", "thinking": "answer is 4", "local": True}], [{"type": "text", "text": "4"}]])
        out = a.run("2+2?")
        self.assertEqual(out["final"], "4")
        self.assertIn("ended without a reply", a.messages[-2]["content"])

    def test_repeat_warning(self):
        same = tool("bash", command="echo x")
        a = self.agent([[same], [same], [same], [{"type": "text", "text": "stop"}]])
        a.run("loop")
        third = a.messages[6]["content"][0]["content"]
        self.assertIn("three times", third)


if __name__ == "__main__":
    unittest.main()


class LiveRootTriggerTests(unittest.TestCase):
    def test_a_relative_path_in_a_live_root_fires_the_guide_written_for_its_absolute_path(self):
        # omarchy-live is rooted at ~/.config/hypr, where the model writes `bindings.lua`; the guide
        # matches `\.config/hypr/` and never fired on the bare name
        home = Path(tempfile.mkdtemp(prefix="h0-live-"))
        try:
            root = home / ".config" / "hypr"
            root.mkdir(parents=True)
            (root / "bindings.lua").write_text("-- bindings\n")
            fit = Fitting(name="live", root=root, triggers=[
                {"id": "hypr-guide", "on": "before:*", "match": "\\.config/hypr/", "action": "inject", "note": "READ THE HYPR GUIDE"},
                {"id": "lua", "on": "after:write", "match": "\\.lua$", "action": "inject", "note": "LUA WRITTEN"}])
            s = config.Settings(model="fake", headless=True)
            pol = Policy(root=root, headless=True)
            a = Agent(settings=s, provider=Fake([[tool("read", path="bindings.lua")],
                                                  [tool("write", path="new.lua", content="-- x\n")]]),
                      toolbox=Toolbox(root, pol, Sandbox(root=root)), policy=pol, trace=Trace(None), ui=QuietUI(),
                      system="sys", fitting=fit)
            a.run("add a binding")
            results = " ".join(str(b.get("content")) for m in a.messages if m["role"] == "user" and isinstance(m["content"], list)
                               for b in m["content"] if b.get("type") == "tool_result")
            self.assertIn("READ THE HYPR GUIDE", results)
            self.assertIn("LUA WRITTEN", results)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class FittingDirPlaceholder(unittest.TestCase):
    def test_a_fitting_can_name_its_own_files_wherever_it_is_installed(self):
        from h0 import fitting as fm
        d = Path(tempfile.mkdtemp(prefix="h0-fitdir-"))
        try:
            (d / "fitting.toml").write_text('name = "x"\nroot = "/tmp"\n[checks.preview]\nrun = "python3 {fitting_dir}/preview/preview.py"\n'
                                           '[policy]\nro_paths = ["{fitting_dir}/preview"]\n')
            f = fm.load(d)
            real = os.path.realpath(d)
            self.assertEqual(f.checks["preview"]["run"], f"python3 {real}/preview/preview.py")
            self.assertEqual(f.policy["ro_paths"], [f"{real}/preview"])
        finally:
            shutil.rmtree(d, ignore_errors=True)
